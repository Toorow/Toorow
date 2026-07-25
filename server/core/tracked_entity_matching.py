"""toorow -- Inbound alias matching + governed new-entity alert (Story 40.3).

The INBOUND HALF of Epic 40 (competitor / brand registry, E40-AD2). It SPECIALISES Epic
27's value-conformance resolver (Story 27.4, dimension_conformance.py: exact -> normalized
-> similarity via difflib) onto BRAND matching, reuses the 40.1 entity master
(tracked_entity_registry.py, app.tracked_entities + aliases) as the alias corpus, and routes
every new-entity proposal and every alias/negative-alias curation through the Epic 13
draft->approved governance. It does NOT refork the resolver and it declares NO second
approval engine. Four capabilities and nothing more:

  1. A PURE brand-matching pipeline (match_brand): exact -> normalized -> similarity over an
     in-memory org corpus, returning a ranked, provenance-carrying result. A CONFIDENT single
     match resolves (score >= threshold AND leads the runner-up by > ambiguity_band); a
     below-threshold / no-match / ambiguous input resolves to a typed GAP (OUTCOME_ALERT,
     fail-closed, E40-NFR02) -- never a silent join or drop.
  2. The inbound resolution surface (resolve_inbound_brands): given the raw brand strings a
     parsed inbound file carries (an EXPLICIT caller input -- the 27.4 conform_value doctrine;
     the 38.6+ parsed-file wiring is a documented seam, NOT built here) and the
     (project_id -> org_id) anchor, it matches each string, writes an append-only
     match-decision ledger row per string (provenance), and opens a governed alert for each
     residue string.
  3. The governed new-entity alert lifecycle (open/list/approve/dismiss): a below-threshold /
     no-match / ambiguous string opens ONE alert per unknown normalized string per org. The
     entity enters the master 'approved' ONLY on a human approve_new_entity_alert -- nothing
     enters autonomously (E40-AD5). The AI (host LLM, via a 40.5 MCP tool) DRAFTS proposals; a
     human ratifies.
  4. The mis-match correction feedback (correct_match): a human correcting an auto-assigned
     entity adds an alias on the CORRECT entity, or a NEGATIVE alias for a homonym (a string
     that must NOT match a given entity), both audited and governed -- never an autonomous
     silent AI write (E40-FR09, E40-AD5).

LLM PLACEMENT (hard architectural fact): toorow has NO server-side LLM SDK. The LLM lives in
the MCP host (Claude), not the backend -- exactly as Epic 35's daily insights ship a recipe
the host executes and Epic 12/13 mapping corrections flow through a governed proposal. This
module does DETERMINISTIC ranking + a governed proposal surface; the host LLM adjudicates the
ambiguous residue (the alerts) and DRAFTS proposals via 40.5 tools; a human ratifies. There
is NO Anthropic/OpenAI import, NO model id and NO model secret here (a server-side model call
would be an epic-review REJECT, contradicting the no-model-secret posture and the
daily-insights precedent).

REUSE, DO NOT FORK (E40-NFR05, E40-AD2): normalize_value / similarity /
DEFAULT_SIMILARITY_THRESHOLD are IMPORTED from core.dimension_conformance (27.4);
normalize_alias / add_alias / list_tracked_entities_by_org / upsert_tracked_entity /
assert_entity_in_org / EntityNotFound and the socle re-exports (_mint_id,
_write_semantics_audit) are IMPORTED from core.tracked_entity_registry (40.1). This module
declares NO normalizer, NO ratio function, NO audit writer of its own. It ADDS functions in a
NEW sibling module and does NOT change 40.1's tracked_entity_registry.py behaviour.

AD-2 (ZERO provider vocabulary): this module contains NO connector name and NO brand name.
Canonical names, aliases and strings are org/project DATA, never code. The ONLY vocabulary is
the METHOD_* / REASON_* / OUTCOME_* / STATUS_* product constants mirroring the SQL CHECKs
(tolerated by the AD-2 grep, exactly like dimension_conformance's constants).

Design mirrors dimension_conformance.py / tracked_entity_registry.py: ``from __future__
import annotations``, module logger, lazy ``core.db`` imports inside the DB functions, pure
functions kept separate from I/O so the invariants are testable without Postgres.

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.dimension_conformance import (  # 27.4 -- reuse the resolver's building blocks
    DEFAULT_SIMILARITY_THRESHOLD,
    normalize_value,
    similarity,
)
from core.tracked_entity_registry import (  # noqa: F401 -- 40.1 socle re-exports
    EntityNotFound,
    _mint_id,
    _write_semantics_audit,
    add_alias,
    assert_entity_in_org,
    list_tracked_entities_by_org,
    normalize_alias,
    upsert_tracked_entity,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named so they are configurable later, per the house pattern).
#
# These product literals are the ONLY vocabulary in this module and mirror the SQL CHECKs of
# migration 090 -- the AD-2 grep tolerates them explicitly (like dimension_conformance's
# METHOD_*/STATUS_* constants). No provider/brand name lives here.
# ---------------------------------------------------------------------------

# Match methods (mirror the CHECK on method in migration 090; same family as 27.4).
METHOD_EXACT = "exact"
METHOD_NORMALIZED = "normalized"
METHOD_SIMILARITY = "similarity"

# Alert reasons (mirror the CHECK on alert_reason / reason in migration 090).
REASON_NO_MATCH = "no_match"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_AMBIGUOUS = "ambiguous"

# Decision outcomes (mirror the CHECK on outcome in migration 090).
OUTCOME_RESOLVED = "resolved"
OUTCOME_ALERT = "alert"

# Alert statuses (mirror the CHECK on status in migration 090).
ALERT_STATUS_OPEN = "open"
ALERT_STATUS_APPROVED = "approved"
ALERT_STATUS_DISMISSED = "dismissed"

# Confidence threshold: REUSE 27.4's 0.88 (a single source of truth for "confident enough").
DEFAULT_CONFIDENCE_THRESHOLD = DEFAULT_SIMILARITY_THRESHOLD  # 0.88

# Ambiguity band: a similarity winner must lead the runner-up by MORE than this, else the
# match is ambiguous (a clear single winner is required -- the homonym/tie guard, E40-NFR02).
DEFAULT_AMBIGUITY_BAND = 0.03

# entity_type values for the shared audit (app.metric_semantics_audit, 049).
NEG_ALIAS_ENTITY_TYPE = "entity_negative_alias"
ALERT_ENTITY_TYPE = "tracked_entity_alert"
MATCH_ENTITY_TYPE = "tracked_entity_match"

# Audit action verbs (the shared writer composes '<entity_type>.<verb>').
ACTION_OPENED = "opened"
ACTION_APPROVED = "approved"
ACTION_DISMISSED = "dismissed"
ACTION_ADDED = "added"
ACTION_REMOVED = "removed"

_NEG_ALIAS_PREFIX = "nena_"
_ALERT_PREFIX = "tea_"
_MATCH_PREFIX = "ibm_"

# The status the entity master carries while a new-entity alert is only PROPOSED vs APPROVED.
_ENTITY_STATUS_DRAFT = "draft"
_ENTITY_STATUS_APPROVED = "approved"


# ---------------------------------------------------------------------------
# Typed errors (callers/endpoints map these; core never raises HTTP here).
# EntityNotFound is re-exported from 40.1 (existence-hiding on a foreign-org entity id).
# ---------------------------------------------------------------------------


class BrandMatchingError(Exception):
    """Base for inbound brand-matching errors."""


class AlertNotFound(BrandMatchingError):
    """Existence-hiding: a foreign-org alert id is indistinguishable from absent (S-2)."""


class MatchDecisionNotFound(BrandMatchingError):
    """A correction call targeted a decision id that does not exist / is foreign (S-2)."""


class CorrectionTargetError(BrandMatchingError):
    """correct_match requires EXACTLY ONE of correct_entity_id / negative_for_entity_id."""


# ===========================================================================
# B.3  Pure matcher (offline-testable -- AC4, AC5). NO I/O.
# ===========================================================================


@dataclass(frozen=True)
class BrandCandidate:
    """One ranked candidate for a raw string (PURE data)."""

    entity_id: str
    matched_alias: str          # the corpus string (canonical_name or alias) that scored
    method: str                 # exact | normalized | similarity
    score: float                # 1.0 for exact/normalized; difflib ratio for similarity


@dataclass(frozen=True)
class BrandMatch:
    """The result of matching one raw string (PURE data)."""

    outcome: str                # resolved | alert
    raw_string: str
    normalized_string: str
    # resolved:
    entity_id: str | None = None
    matched_alias: str | None = None
    method: str | None = None
    score: float | None = None
    # alert:
    reason: str | None = None
    candidates: tuple[BrandCandidate, ...] = ()   # ranked rejected candidates


@dataclass
class _CorpusEntry:
    """An entity shaped for the matcher: verbatim + normalized keys precomputed once."""

    entity_id: str
    canonical_name: str
    # (verbatim_string, normalized_string) for canonical_name + every alias, order-stable.
    surfaces: tuple[tuple[str, str], ...]
    negative_norms: frozenset[str] = field(default=frozenset())


def _build_corpus(
    entities: list[dict], negatives_by_entity: dict[str, list[str]] | None = None
) -> list[_CorpusEntry]:
    """Shape 40.1 entity rows (+ their negative aliases) into the matcher corpus (PURE).

    Normalized keys are precomputed ONCE. Order is preserved from the input (40.1 lists by
    canonical_name) so the pipeline is order-stable. A negative_norms set holds the normalized
    strings the entity must NOT match (the exclusion set, AC5)."""
    negatives_by_entity = negatives_by_entity or {}
    corpus: list[_CorpusEntry] = []
    for entity in entities:
        entity_id = entity.get("id")
        canonical = entity.get("canonical_name")
        if entity_id is None or canonical is None:
            continue
        surfaces: list[tuple[str, str]] = [(canonical, normalize_value(canonical))]
        seen = {surfaces[0][1]}
        for alias in entity.get("aliases") or []:
            if alias is None:
                continue
            norm = normalize_value(alias)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            surfaces.append((alias, norm))
        neg = {
            n
            for n in (normalize_value(s) for s in negatives_by_entity.get(entity_id, []))
            if n
        }
        corpus.append(
            _CorpusEntry(
                entity_id=entity_id,
                canonical_name=canonical,
                surfaces=tuple(surfaces),
                negative_norms=frozenset(neg),
            )
        )
    return corpus


def _alert(raw: str, norm: str, reason: str, ranked: list[BrandCandidate]) -> BrandMatch:
    """Build an OUTCOME_ALERT result (fail-closed) carrying the ranked rejected candidates."""
    return BrandMatch(
        outcome=OUTCOME_ALERT,
        raw_string=raw,
        normalized_string=norm,
        reason=reason,
        candidates=tuple(ranked),
    )


def _sort_key(entry: _CorpusEntry, score: float) -> tuple:
    """Deterministic stable tie-break: (-score, canonical_name, entity_id)."""
    return (-score, entry.canonical_name, entry.entity_id)


def match_brand(
    raw_string: str,
    corpus: list[dict] | list[_CorpusEntry],
    *,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ambiguity_band: float = DEFAULT_AMBIGUITY_BAND,
) -> BrandMatch:
    """Deterministic exact -> normalized -> similarity match of *raw_string* to the org
    corpus (PURE, no I/O).

    *corpus* is a list of 40.1 entity dicts ({id, canonical_name, aliases,
    negative_aliases?}) OR pre-built _CorpusEntry rows. Negative aliases are excluded FIRST:
    an entity whose negative set contains normalize(raw_string) is removed from that string's
    candidate set (AC5). A string follows the FIRST stage that clears it (exact wins over
    similarity, test 12). A SIMILARITY winner qualifies IFF score >= threshold AND leads the
    runner-up by > ambiguity_band (a clear single winner). Otherwise OUTCOME_ALERT with the
    reason (no_match | below_threshold | ambiguous) and the ranked rejected candidates. Stable
    tie-break (-score, canonical_name, entity_id). Reuses normalize_value / similarity (27.4)
    and normalize_alias (40.1) -- no fork.

    NEVER a silent join and NEVER a silent drop: every input yields either a resolved match or
    an alert (E40-NFR02, fail-closed)."""
    # Accept both raw dicts and pre-built corpus rows.
    if corpus and isinstance(corpus[0], dict):
        entries = _build_corpus(corpus)  # type: ignore[arg-type]
    else:
        entries = list(corpus)  # type: ignore[arg-type]

    norm_raw = normalize_value(raw_string)

    # AC5: exclude any entity whose negative set contains the normalized raw string FIRST.
    candidates_pool = [e for e in entries if norm_raw not in e.negative_norms]

    if not candidates_pool or not norm_raw:
        # No candidate entity at all (empty corpus, all suppressed, or an empty normalized
        # form) -> no_match (fail-closed).
        return _alert(raw_string, norm_raw, REASON_NO_MATCH, [])

    # -- Stage 1: EXACT -- raw string equals a canonical_name or an alias VERBATIM.
    exact_hits: list[BrandCandidate] = []
    for entry in candidates_pool:
        for verbatim, _norm in entry.surfaces:
            if verbatim == raw_string:
                exact_hits.append(
                    BrandCandidate(entry.entity_id, verbatim, METHOD_EXACT, 1.0)
                )
                break
    if exact_hits:
        return _resolve_stage(raw_string, norm_raw, candidates_pool, exact_hits, METHOD_EXACT,
                              threshold, ambiguity_band)

    # -- Stage 2: NORMALIZED -- normalize(raw) equals the normalized form of a surface.
    norm_hits: list[BrandCandidate] = []
    for entry in candidates_pool:
        for verbatim, norm in entry.surfaces:
            if norm == norm_raw:
                norm_hits.append(
                    BrandCandidate(entry.entity_id, verbatim, METHOD_NORMALIZED, 1.0)
                )
                break
    if norm_hits:
        return _resolve_stage(raw_string, norm_raw, candidates_pool, norm_hits,
                              METHOD_NORMALIZED, threshold, ambiguity_band)

    # -- Stage 3: SIMILARITY -- best difflib ratio on the normalized forms.
    sim_hits: list[tuple[_CorpusEntry, BrandCandidate]] = []
    for entry in candidates_pool:
        best_alias = None
        best_score = -1.0
        for verbatim, norm in entry.surfaces:
            if not norm:
                continue
            score = similarity(norm_raw, norm)
            if score > best_score:
                best_score = score
                best_alias = verbatim
        if best_alias is not None:
            sim_hits.append(
                (entry, BrandCandidate(entry.entity_id, best_alias, METHOD_SIMILARITY,
                                       best_score))
            )

    # Rank by the stable tie-break (best first).
    sim_hits.sort(key=lambda pair: _sort_key(pair[0], pair[1].score))
    ranked = [cand for _entry, cand in sim_hits]

    if not ranked:
        return _alert(raw_string, norm_raw, REASON_NO_MATCH, [])

    top = ranked[0]
    if top.score < threshold:
        # A best candidate exists but scores below the confidence threshold -> alert, honest.
        return _alert(raw_string, norm_raw, REASON_BELOW_THRESHOLD, ranked)

    # Above threshold: require a CLEAR single winner (leads the runner-up by > band).
    if len(ranked) >= 2:
        runner_up = ranked[1]
        if top.score - runner_up.score <= ambiguity_band:
            # Two candidates tie above the threshold -> ambiguous (fail-closed homonym guard).
            return _alert(raw_string, norm_raw, REASON_AMBIGUOUS, ranked)

    return BrandMatch(
        outcome=OUTCOME_RESOLVED,
        raw_string=raw_string,
        normalized_string=norm_raw,
        entity_id=top.entity_id,
        matched_alias=top.matched_alias,
        method=top.method,
        score=top.score,
    )


def _resolve_stage(
    raw: str,
    norm: str,
    pool: list[_CorpusEntry],
    hits: list[BrandCandidate],
    method: str,
    threshold: float,
    ambiguity_band: float,
) -> BrandMatch:
    """Resolve an exact/normalized stage. A single hit resolves; two DIFFERENT entities both
    scoring 1.0 on the same string is genuinely ambiguous (a homonym at the exact/normalized
    level -- the SAME string names two org entities) -> alert, fail-closed."""
    distinct_entities = {c.entity_id for c in hits}
    if len(distinct_entities) == 1:
        win = hits[0]
        return BrandMatch(
            outcome=OUTCOME_RESOLVED,
            raw_string=raw,
            normalized_string=norm,
            entity_id=win.entity_id,
            matched_alias=win.matched_alias,
            method=win.method,
            score=win.score,
        )
    # Two distinct entities claim the same string at score 1.0 -> ambiguous (never a silent
    # join to an arbitrary one).
    ranked = sorted(
        hits, key=lambda c: (-c.score, c.matched_alias, c.entity_id)
    )
    return _alert(raw, norm, REASON_AMBIGUOUS, ranked)


# ===========================================================================
# Pure shapers (row <-> dict) split from I/O so the fake-store tests exercise the
# invariants without Postgres, like dimension_conformance / tracked_entity_registry.
# ===========================================================================

_DECISION_COLUMNS = (
    "id, project_id, org_id, import_ref, raw_string, normalized_string, outcome, "
    "matched_entity_id, matched_alias, method, confidence, alert_reason, candidates, "
    "created_by, created_at"
)

_ALERT_COLUMNS = (
    "id, org_id, project_id, raw_string, normalized_string, proposed_entity_id, status, "
    "reason, candidates, created_by, resolved_by, resolved_at, created_at, updated_at"
)

_DECISION_TS_COLS = {"created_at"}
_ALERT_TS_COLS = {"resolved_at", "created_at", "updated_at"}


def _row_to_decision(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _DECISION_TS_COLS and val is not None else val
    return record


def _row_to_alert(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ALERT_TS_COLS and val is not None else val
    return record


def _candidates_json(match: BrandMatch) -> str:
    """Serialize a match's ranked candidates for the ledger/alert JSONB (PURE)."""
    import json  # noqa: PLC0415

    return json.dumps(
        [
            {
                "entity_id": c.entity_id,
                "matched_alias": c.matched_alias,
                "method": c.method,
                "score": c.score,
            }
            for c in match.candidates
        ]
    )


# ===========================================================================
# B.4  Inbound resolution + provenance ledger (DB -- AC1, AC2).
# ===========================================================================


def _project_org_id(conn, project_id: str) -> str | None:
    """Read app.projects.org_id for *project_id* on an EXISTING connection (None if unknown).

    A tiny local read (mirrors tracked_entity_registry._project_org_id) rather than editing
    40.1 to export it -- keeps 40.3 strictly additive (Project Structure Notes)."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _load_negative_aliases(conn, org_id: str) -> dict[str, list[str]]:
    """Return {entity_id -> [normalized negative-alias strings]} for the org (join through
    tracked_entities.org_id). Feeds the matcher corpus exclusion (AC5)."""
    out: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.entity_id, n.normalized_string
            FROM app.entity_negative_aliases n
            JOIN app.tracked_entities e ON e.id = n.entity_id
            WHERE e.org_id = %s
            """,
            (org_id,),
        )
        for entity_id, normalized_string in cur.fetchall():
            out.setdefault(entity_id, []).append(normalized_string)
    return out


def _insert_decision(conn, *, project_id, org_id, import_ref, match, identity) -> dict:
    """INSERT one append-only provenance ledger row for a matched string. Returns the row."""
    new_id = _mint_id(_MATCH_PREFIX)
    is_resolve = match.outcome == OUTCOME_RESOLVED
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.inbound_brand_match_decisions
                (id, project_id, org_id, import_ref, raw_string, normalized_string, outcome,
                 matched_entity_id, matched_alias, method, confidence, alert_reason,
                 candidates, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now())
            RETURNING {_DECISION_COLUMNS}
            """,
            (
                new_id,
                project_id,
                org_id,
                import_ref,
                match.raw_string,
                match.normalized_string,
                match.outcome,
                match.entity_id if is_resolve else None,
                match.matched_alias if is_resolve else None,
                match.method if is_resolve else None,
                match.score if is_resolve else None,
                None if is_resolve else match.reason,
                _candidates_json(match),
                identity,
            ),
        )
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description]
    return _row_to_decision(cols, row)


def resolve_inbound_brands(
    *,
    project_id: str,
    raw_strings: list[str],
    identity: str,
    import_ref: str,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ambiguity_band: float = DEFAULT_AMBIGUITY_BAND,
) -> dict:
    """Resolve the raw brand strings of ONE inbound import against the project's org corpus.

    Raw strings are an EXPLICIT INPUT (27.4 conform_value doctrine -- the warehouse-read
    wiring is 38.6+, a documented seam, NOT built here). Steps, all org-scoped: resolve
    project_id -> org_id; load the corpus (list_tracked_entities_by_org + the negative-alias
    exclusion set); match_brand per string; WRITE one append-only inbound_brand_match_decisions
    row per string (provenance, AC1); for each ALERT open-or-reuse a tracked_entity_alert
    idempotently on (org_id, normalized_string, status='open'). Returns
    {resolved: [...], alerts: [...], counts: {resolved, alert}}.

    This is the function the 38.6+ parsed-file import CALLS -- 40.3 documents the seam, does
    NOT wire a non-existent parsed-row table. NEVER a silent join/drop (E40-NFR02). A match RUN
    NEVER mutates the master (no add_alias / upsert_tracked_entity here -- E40-AD5); it only
    WRITES ledger/alert rows and READS aliases."""
    from core.db import get_connection  # noqa: PLC0415

    org_id = _resolve_org(project_id)

    # Load the org corpus + negative aliases ONCE for the whole import. AD-5: only RATIFIED
    # (status='approved') entities are match candidates -- a not-yet-approved draft (e.g. a future
    # 40.5 proposal) must NEVER auto-resolve an inbound string to something no human approved.
    entities = list_tracked_entities_by_org(org_id, status="approved")
    with get_connection() as conn:
        negatives = _load_negative_aliases(conn, org_id)
    corpus = _build_corpus(entities, negatives)

    resolved: list[dict] = []
    alerts: list[dict] = []
    with get_connection() as conn:
        for raw in raw_strings:
            match = match_brand(
                raw, corpus, threshold=threshold, ambiguity_band=ambiguity_band
            )
            decision = _insert_decision(
                conn,
                project_id=project_id,
                org_id=org_id,
                import_ref=import_ref,
                match=match,
                identity=identity,
            )
            if match.outcome == OUTCOME_RESOLVED:
                resolved.append(decision)
            else:
                alert = _open_alert_on_conn(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    match=match,
                    identity=identity,
                )
                alerts.append(alert)
        conn.commit()

    logger.info(
        "tracked_entity_matching: resolved import_ref=%s resolved=%d alert=%d",
        import_ref,
        len(resolved),
        len(alerts),
    )
    return {
        "resolved": resolved,
        "alerts": alerts,
        "counts": {"resolved": len(resolved), "alert": len(alerts)},
    }


def _resolve_org(project_id: str) -> str:
    """Resolve project_id -> org_id (fail-closed: an unknown project raises EntityNotFound,
    existence-hiding, never a silent default org)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        org_id = _project_org_id(conn, project_id)
    if org_id is None:
        raise EntityNotFound(f"project not found: {project_id!r}")
    return org_id


# ===========================================================================
# B.5  Alert lifecycle (governed draft->approved -- AC2, E40-AD5).
# ===========================================================================


def _select_alert_by_id(conn, alert_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_ALERT_COLUMNS} FROM app.tracked_entity_alerts WHERE id = %s",
            (alert_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_alert(cols, row)


def _select_open_alert(conn, *, org_id, normalized_string) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ALERT_COLUMNS}
            FROM app.tracked_entity_alerts
            WHERE org_id = %s AND normalized_string = %s AND status = %s
            """,
            (org_id, normalized_string, ALERT_STATUS_OPEN),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_alert(cols, row)


def _open_alert_on_conn(conn, *, org_id, project_id, match: BrandMatch, identity) -> dict:
    """Open-or-reuse a tracked_entity_alert for an ALERT match on an EXISTING transaction.

    Idempotent on (org_id, normalized_string, status='open'): a re-import of the same unknown
    string REUSES the existing open alert (no duplicate, AC2). Does NOT create the master
    entity (that is the human approve). Audits only when a NEW alert is opened."""
    existing = _select_open_alert(conn, org_id=org_id, normalized_string=match.normalized_string)
    if existing is not None:
        return existing

    new_id = _mint_id(_ALERT_PREFIX)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.tracked_entity_alerts
                (id, org_id, project_id, raw_string, normalized_string, proposed_entity_id,
                 status, reason, candidates, created_by, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s::jsonb, %s, now(), now())
            ON CONFLICT (org_id, normalized_string) WHERE status = 'open' DO NOTHING
            RETURNING {_ALERT_COLUMNS}
            """,
            (
                new_id,
                org_id,
                project_id,
                match.raw_string,
                match.normalized_string,
                ALERT_STATUS_OPEN,
                match.reason,
                _candidates_json(match),
                identity,
            ),
        )
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description]
    # Concurrency: a parallel import of the same unknown lost the race -> ON CONFLICT DID NOTHING
    # (no row returned). Re-SELECT the winner's open alert and REUSE it (no audit -- not newly
    # opened). This turns a would-be UniqueViolation crash into idempotent open-or-reuse.
    if row is None:
        existing = _select_open_alert(
            conn, org_id=org_id, normalized_string=match.normalized_string
        )
        if existing is not None:
            return existing
        # Extremely unlikely (conflict then the row vanished): fail-closed, re-raise on retry.
        raise RuntimeError("tracked_entity_alerts: open-alert conflict but no open row found")
    after = _row_to_alert(cols, row)

    _write_semantics_audit(
        conn,
        identity=identity,
        action=ACTION_OPENED,
        entity_type=ALERT_ENTITY_TYPE,
        entity_id=after["id"],
        scope_level="ORG",
        org_id=org_id,
        project_id=None,
        before=None,
        after=after,
    )
    return after


def open_new_entity_alert(*, org_id: str, project_id: str, match: BrandMatch,
                          identity: str) -> dict:
    """Open-or-reuse a tracked_entity_alert for an ALERT match (idempotent on the open
    unicity) + audit. Does NOT create the master entity (that is the human approve). A
    re-import of the same unknown string reuses the open alert."""
    from core.db import get_connection  # noqa: PLC0415

    if match.outcome != OUTCOME_ALERT:
        raise BrandMatchingError("open_new_entity_alert expects an OUTCOME_ALERT match")
    with get_connection() as conn:
        alert = _open_alert_on_conn(
            conn, org_id=org_id, project_id=project_id, match=match, identity=identity
        )
        conn.commit()
    return alert


def list_open_alerts(*, org_id: str | None = None,
                     project_id: str | None = None) -> list[dict]:
    """List OPEN alerts by org (owner/admin triage) or by project (who saw it), newest first.

    Exactly one of org_id / project_id must be supplied (the caller's authorised scope)."""
    from core.db import get_connection  # noqa: PLC0415

    if (org_id is None) == (project_id is None):
        raise BrandMatchingError("list_open_alerts requires exactly one of org_id/project_id")
    column = "org_id" if org_id is not None else "project_id"
    value = org_id if org_id is not None else project_id
    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_ALERT_COLUMNS}
                FROM app.tracked_entity_alerts
                WHERE {column} = %s AND status = %s
                ORDER BY created_at DESC
                """,
                (value, ALERT_STATUS_OPEN),
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_alert(cols, row))
    return rows


def approve_new_entity_alert(*, alert_id: str, canonical_name: str, identity: str,
                             org_id: str) -> dict:
    """HUMAN-INVOKED approval (governed draft->approved).

    Asserts the alert is in *org_id* (existence-hiding -> AlertNotFound), mints the entity via
    40.1's upsert_tracked_entity(status='approved', created_by=identity) with the alert's
    raw_string as its first alias, stamps resolved_by/resolved_at + status='approved' on the
    alert, and audits. The entity entered the master ONLY here, on a recorded human decision
    (E40-AD5). NEVER called autonomously -- the 40.5 MCP tool is governance-gated +
    human-confirmed."""
    from core.db import get_connection  # noqa: PLC0415

    # Existence-hiding + fetch the raw_string outside the mutation transaction.
    with get_connection() as conn:
        alert = _select_alert_by_id(conn, alert_id)
    if alert is None or alert.get("org_id") != org_id:
        raise AlertNotFound(f"tracked_entity_alert not found: {alert_id!r}")
    # A recorded human decision is FINAL (E40-AD5): only an OPEN alert is approvable. A
    # dismissed/approved alert must NEVER be silently revived into a master entity -- reject
    # before minting anything.
    if alert.get("status") != ALERT_STATUS_OPEN:
        raise AlertNotFound(f"tracked_entity_alert not open: {alert_id!r}")

    # Mint the master entity (its own audit, entity_type=tracked_entity) -- draft->approved:
    # the entity enters the master 'approved' ONLY on this human call. The raw_string is its
    # first alias so the same string resolves next time.
    entity = upsert_tracked_entity(
        org_id=org_id,
        canonical_name=canonical_name,
        created_by=identity,
        aliases=[alert["raw_string"]],
        status=_ENTITY_STATUS_APPROVED,
    )

    with get_connection() as conn:
        before = _select_alert_by_id(conn, alert_id)
        if before is None or before.get("org_id") != org_id:
            raise AlertNotFound(f"tracked_entity_alert not found: {alert_id!r}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.tracked_entity_alerts
                SET status = %s, proposed_entity_id = %s, resolved_by = %s,
                    resolved_at = now(), updated_at = now()
                WHERE id = %s AND status = %s
                RETURNING {_ALERT_COLUMNS}
                """,
                (ALERT_STATUS_APPROVED, entity["id"], identity, alert_id, ALERT_STATUS_OPEN),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        # Race guard: the alert was concurrently resolved between the early check and here. The
        # entity mint is idempotent on canonical_name (no duplicate), so this is safe to reject.
        if row is None:
            raise AlertNotFound(f"tracked_entity_alert not open: {alert_id!r}")
        after = _row_to_alert(cols, row)
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_APPROVED,
            entity_type=ALERT_ENTITY_TYPE,
            entity_id=after["id"],
            scope_level="ORG",
            org_id=org_id,
            project_id=None,
            before=before,
            after=after,
        )
        conn.commit()
    return {"alert": after, "entity": entity}


def dismiss_alert(*, alert_id: str, identity: str, org_id: str) -> dict:
    """Human dismissal (a false alert / a non-brand string): status='dismissed' + audit.

    The string is honestly recorded as not-a-new-entity; NOTHING enters the master. Asserts
    the alert is in *org_id* (existence-hiding -> AlertNotFound)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_alert_by_id(conn, alert_id)
        if before is None or before.get("org_id") != org_id:
            raise AlertNotFound(f"tracked_entity_alert not found: {alert_id!r}")
        # Only an OPEN alert is dismissable (E40-AD5: a prior human decision is final -- do not
        # re-transition an already approved/dismissed alert).
        if before.get("status") != ALERT_STATUS_OPEN:
            raise AlertNotFound(f"tracked_entity_alert not open: {alert_id!r}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.tracked_entity_alerts
                SET status = %s, resolved_by = %s, resolved_at = now(), updated_at = now()
                WHERE id = %s AND status = %s
                RETURNING {_ALERT_COLUMNS}
                """,
                (ALERT_STATUS_DISMISSED, identity, alert_id, ALERT_STATUS_OPEN),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        if row is None:
            raise AlertNotFound(f"tracked_entity_alert not open: {alert_id!r}")
        after = _row_to_alert(cols, row)
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DISMISSED,
            entity_type=ALERT_ENTITY_TYPE,
            entity_id=after["id"],
            scope_level="ORG",
            org_id=org_id,
            project_id=None,
            before=before,
            after=after,
        )
        conn.commit()
    return after


# ===========================================================================
# B.6  Negative aliases + mis-match correction (AC3).
# ===========================================================================


def _select_negative_alias(conn, *, entity_id, normalized_string) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_id, raw_string, normalized_string, created_by, created_at
            FROM app.entity_negative_aliases
            WHERE entity_id = %s AND normalized_string = %s
            """,
            (entity_id, normalized_string),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return {c: v for c, v in zip(cols, row)}


def add_negative_alias(entity_id: str, raw_string: str, *, identity: str) -> dict:
    """Record 'raw_string must NOT match entity_id' (a homonym) + audit.

    De-dupes on the normalized form (reuse normalize_alias): re-adding an existing suppression
    is a NO-OP (no audit). Raises EntityNotFound on an unknown id (existence-hiding on reads --
    an unknown id is simply absent). Human-authored ONLY -- a match RUN never calls this
    (E40-AD5)."""
    from core.db import get_connection  # noqa: PLC0415

    normalized = normalize_alias(raw_string)
    with get_connection() as conn:
        # An unknown entity id -> EntityNotFound (existence-hiding). Capture the entity's org so
        # the ORG-scoped audit carries the real org_id (honest provenance, not NULL).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM app.tracked_entities WHERE id = %s", (entity_id,)
            )
            _erow = cur.fetchone()
            if _erow is None:
                raise EntityNotFound(f"tracked_entity not found: {entity_id!r}")
            entity_org_id = _erow[0]

        existing = _select_negative_alias(
            conn, entity_id=entity_id, normalized_string=normalized
        )
        if existing is not None:
            return existing  # no-op: no INSERT, no audit.

        new_id = _mint_id(_NEG_ALIAS_PREFIX)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.entity_negative_aliases
                    (id, entity_id, raw_string, normalized_string, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, now())
                RETURNING id, entity_id, raw_string, normalized_string, created_by, created_at
                """,
                (new_id, entity_id, raw_string, normalized, identity),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = {c: v for c, v in zip(cols, row)}
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_ADDED,
            entity_type=NEG_ALIAS_ENTITY_TYPE,
            entity_id=after["id"],
            scope_level="ORG",
            org_id=entity_org_id,
            project_id=None,
            before=None,
            after={k: v for k, v in after.items() if k != "created_at"},
        )
        conn.commit()
    return after


def remove_negative_alias(entity_id: str, raw_string: str, *, identity: str) -> bool:
    """Remove a negative alias (matched on the normalized form) + audit. Returns True if a
    row went; removing an absent suppression is a NO-OP (no audit). Raises EntityNotFound if
    the entity id is unknown (existence-hiding)."""
    from core.db import get_connection  # noqa: PLC0415

    normalized = normalize_alias(raw_string)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM app.tracked_entities WHERE id = %s", (entity_id,)
            )
            _erow = cur.fetchone()
            if _erow is None:
                raise EntityNotFound(f"tracked_entity not found: {entity_id!r}")
            entity_org_id = _erow[0]
        before = _select_negative_alias(
            conn, entity_id=entity_id, normalized_string=normalized
        )
        if before is None:
            return False  # no-op.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.entity_negative_aliases WHERE id = %s", (before["id"],)
            )
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_REMOVED,
            entity_type=NEG_ALIAS_ENTITY_TYPE,
            entity_id=before["id"],
            scope_level="ORG",
            org_id=entity_org_id,
            project_id=None,
            before={k: v for k, v in before.items() if k != "created_at"},
            after=None,
        )
        conn.commit()
    return True


def _select_decision_by_id(conn, decision_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_DECISION_COLUMNS} FROM app.inbound_brand_match_decisions WHERE id = %s",
            (decision_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_decision(cols, row)


def correct_match(
    *,
    decision_id: str,
    raw_string: str,
    project_id: str,
    identity: str,
    correct_entity_id: str | None = None,
    negative_for_entity_id: str | None = None,
) -> dict:
    """The AC3 mis-match feedback (EXACTLY ONE of the two targets):

      * correct_entity_id       -> add_alias(correct_entity_id, raw_string) (the string
                                   resolves to the RIGHT entity next time), OR
      * negative_for_entity_id  -> add_negative_alias(negative_for_entity_id, raw_string)
                                   (a homonym the wrong entity must not claim).

    Guards the caller's org owns BOTH the decision and the target entity (existence-hiding:
    a foreign-org decision / entity is indistinguishable from absent). Audits (via the
    delegated add_alias / add_negative_alias). NEVER an autonomous silent AI write -- this is
    an explicit human correction (E40-FR09, E40-AD5)."""
    if (correct_entity_id is None) == (negative_for_entity_id is None):
        raise CorrectionTargetError(
            "correct_match requires EXACTLY ONE of correct_entity_id / negative_for_entity_id"
        )

    # The caller's org anchors both ownership checks (existence-hiding on a foreign id).
    org_id = _resolve_org(project_id)

    # The decision must belong to the caller's org (existence-hiding).
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        decision = _select_decision_by_id(conn, decision_id)
    if decision is None or decision.get("org_id") != org_id:
        raise MatchDecisionNotFound(
            f"inbound_brand_match_decision not found: {decision_id!r}"
        )

    target_id = correct_entity_id or negative_for_entity_id
    # assert_entity_in_org raises EntityNotFound on a foreign-org / absent id (S-2).
    assert_entity_in_org(target_id, org_id)  # type: ignore[arg-type]

    if correct_entity_id is not None:
        result = add_alias(correct_entity_id, raw_string, identity=identity)
        return {"kind": "alias", "entity": result}
    result = add_negative_alias(negative_for_entity_id, raw_string, identity=identity)  # type: ignore[arg-type]
    return {"kind": "negative_alias", "negative_alias": result}
