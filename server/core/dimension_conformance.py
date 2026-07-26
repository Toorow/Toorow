"""toorow -- Conformed dimensions: value-level unification engine (Story 27.4).

The VALUE-level counterpart of the metric-semantics socle (Epic 27). The manifests
carry the SCHEMA-level canonical_dimension_mapping (source_dim -> canonical_dim); this
module carries the VALUE-level mapping:

    (canonical_dimension, connector, source_value) -> canonical_value

so the SAME dimension value named differently by several connectors resolves to ONE
canonical value. The source_value is exactly a fact_daily_kpi.breakdown_value; the
canonical_dimension is a client-chosen conformed-dimension label. No warehouse row is
read or written here -- the values to reconcile are SUPPLIED BY THE CALLER
(values_by_connector); the "read distinct values from the warehouse" wiring is deferred
(Story 27.4 Hors-perimetre).

Semi-auto suggestion, human validation:
  * a three-stage PURE pipeline exact -> normalized -> similarity (difflib stdlib)
    proposes mappings; ALL outputs are status='proposed' (NEVER auto-confirmed, even
    exact/normalized -- they are only a higher CONFIDENCE, not a blank cheque).
  * only confirm_mapping / set_mapping_manual produce status='confirmed'.
  * conform_value resolves ONLY 'confirmed' rows (proposed/rejected/absent -> None).
    This is AD-9 applied to dimensions: no silent value fusion of an un-validated
    suggestion.

Resolution is a cascade PROJECT > ORG > PLATFORM (org = anchor, Jean's decision): the
most specific confirmed row wins, pair by pair, deterministically. Every mutation writes
one append-only audit row (app.metric_semantics_audit, migration 049) in the SAME
transaction as the mutation, with entity_type='dimension_value_mapping'.

REUSE of 27.1 (import, NOT copy): validate_scope, _mint_id, _write_semantics_audit are
IMPORTED from core.metric_semantics (single source of truth for scope validation, ULID
minting and the audit writer). Decision (Completion Notes): a direct import of the
underscore helpers is chosen over a local wrapper -- they are stable socle symbols
(27.1) and copy-pasting the audit logic would fork the single source of truth. See
metric_semantics.py for their contract.

AD-2 (ZERO provider vocabulary): this module contains NO connector name and NO
dimension name. Connector/dimension labels come from the caller / DB rows, never from
code.

Design mirrors metric_semantics.py: ``from __future__ import annotations``, module
logger, lazy ``core.db`` imports inside the DB functions, prefixed-ULID mint helper,
pure functions kept separate from I/O so the invariants are testable without Postgres.

Story 27.9 EXTENSION (section C at the end of the module): the CLIENT-OWNED LABEL.
canonical_dimension is a STABLE INTERNAL IDENTIFIER (it is written into mapping rows,
plan payloads and kept renders -- renaming it would break the lineage), so the readable
name lives in its own table (app.dimension_labels, migration 106) and resolves through
the SAME cascade PROJECT > ORG > PLATFORM. Absent label -> the identifier is shown AND
the fallback is declared (label_source='fallback_identifier'); nothing is invented.

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from core.metric_semantics import (  # noqa: F401 -- re-exported socle symbols (27.1)
    SCOPE_ORG,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    InvalidScope,
    _mint_id,
    _write_semantics_audit,
    validate_scope,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named so they are configurable later, per the house pattern).
# ---------------------------------------------------------------------------

# Specificity order for the cascade (least -> most specific). Mirrors 27.1.
_SCOPE_RANK = {SCOPE_PLATFORM: 0, SCOPE_ORG: 1, SCOPE_PROJECT: 2}

# Suggestion methods (mirror the CHECK on suggestion_method in migration 052).
METHOD_EXACT = "exact"
METHOD_NORMALIZED = "normalized"
METHOD_SIMILARITY = "similarity"
METHOD_MANUAL = "manual"

# Statuses (mirror the CHECK on status in migration 052).
STATUS_PROPOSED = "proposed"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"

# Default similarity threshold for the difflib stage (prudent; configurable per call).
DEFAULT_SIMILARITY_THRESHOLD = 0.88

# Audit action verbs (the shared writer composes '<entity_type>.<verb>').
ACTION_CREATED = "created"
ACTION_UPSERTED = "upserted"
ACTION_CONFIRMED = "confirmed"
ACTION_REJECTED = "rejected"
ACTION_DELETED = "deleted"

# entity_type for the shared audit (app.metric_semantics_audit, 049).
ENTITY_TYPE = "dimension_value_mapping"

_ID_PREFIX = "dvm_"

# Regexes for normalization (compiled once; PURE, no provider/dimension vocabulary).
# Bracket/parenthesis/brace tags anywhere in the string, e.g. '[FR] ', '(2026)', '{promo}'.
_BRACKET_TAG_RE = re.compile(r"[\[\(\{][^\[\]\(\)\{\}]*[\]\)\}]")
# Common datestamps -- DATE-SHAPED tokens ONLY, never an arbitrary numeric id (F-2):
#   * 'YYYY-MM-DD' / 'YYYY/MM/DD'                     (separated calendar dates)
#   * 'YYYYMMDD' (8 digits) / 'YYYYMM' (6 digits)     ONLY when the month (and day) are
#     valid -- '20260731', '202601' strip; a bare '12345678'/'123456' id does NOT.
#   * 'YYYY' plausible years only ('19xx' / '20xx')   -- '1234'/'5678' ids stay intact.
# and quarter stamps 'Q3 2026' / 'Q3-2026' / '2026 Q3'.
_QUARTER_RE = re.compile(r"\bq[1-4][\s\-_]?\d{4}\b|\b\d{4}[\s\-_]?q[1-4]\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(19|20)\d{2}[-/]\d{2}[-/]\d{2}\b"                      # yyyy-mm-dd / yyyy/mm/dd
    r"|\b(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b"  # yyyymmdd (valid month+day)
    r"|\b(19|20)\d{2}(0[1-9]|1[0-2])\b"                       # yyyymm  (valid month)
    r"|\b(19|20)\d{2}\b"                                      # yyyy    (plausible year only)
)
# Separators collapsed to a single space: '-', '_', '.', and runs of whitespace.
_SEPARATOR_RE = re.compile(r"[\-_.]+")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Typed errors (callers/endpoints map these; core never raises HTTP here).
# InvalidScope is re-exported from metric_semantics (single source of truth).
# ---------------------------------------------------------------------------


class DimensionConformanceError(Exception):
    """Base for conformed-dimension validation errors."""


class MappingNotFound(DimensionConformanceError):
    """A curation call targeted a mapping id that does not exist."""


class ChainedMappingError(DimensionConformanceError):
    """A confirm/manual would create a value-mapping CHAIN or CYCLE (D-3).

    Value conformance is a FLAT one-hop relation: source_value -> canonical_value, and a
    canonical value must be a terminal (it is never itself a source_value that maps
    elsewhere). Confirming A->B while B->C already exists (a chain), or while X->A already
    exists (A as a canonical becoming a source, i.e. a chain the other way / a cycle when
    it loops back), would make resolution ambiguous and non-idempotent. We refuse it and
    ask the human to point every source at the SAME terminal canonical instead."""


# ---------------------------------------------------------------------------
# B.1 Normalization & suggestion -- PURE functions (no DB, offline-testable).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizeOptions:
    """Deterministic normalization knobs. All PURE; no auto-detection in v1.

    strip_affixes: prefixes/suffixes supplied by the CALLER (v1 does NOT auto-detect
    recurring affixes -- Story 27.4 Hors-perimetre). Each affix is stripped from the head
    OR the tail of the (already lowercased/collapsed) form when present.
    """

    lowercase: bool = True
    collapse_separators: bool = True          # '-', '_', '.', multiple spaces -> one space
    strip_bracket_tags: bool = True           # remove '[FR] ', '(2026)', '{promo}'
    strip_datestamps: bool = True             # remove 'YYYY-MM-DD', 'YYYYMM', 'Q3 2026'...
    strip_diacritics: bool = True             # 'Ete' == 'Ete' (accents folded)
    strip_affixes: tuple[str, ...] = ()       # caller-supplied recurring affixes


def normalize_value(value: str, opts: NormalizeOptions = NormalizeOptions()) -> str:
    """Return a deterministic normalized form of *value* (PURE, no I/O).

    Never loses the original (the caller keeps it); serves as the 'normalized' matching
    key and as the persisted normalized_form. Idempotent:
    normalize(normalize(x)) == normalize(x).

    Order matters: tags/datestamps are removed on the RAW-ish text first (they rely on
    bracket/word boundaries), then diacritics/case/separators are folded, then the
    caller affixes are stripped, then the whitespace is collapsed and trimmed.
    """
    if value is None:
        return ""
    text = value

    if opts.strip_bracket_tags:
        text = _BRACKET_TAG_RE.sub(" ", text)
    if opts.strip_datestamps:
        text = _QUARTER_RE.sub(" ", text)
        text = _DATE_RE.sub(" ", text)
    if opts.strip_diacritics:
        # Decompose then drop the combining marks (NFKD) -> ASCII-folded letters.
        text = "".join(
            ch for ch in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(ch)
        )
    if opts.lowercase:
        text = text.lower()
    if opts.collapse_separators:
        text = _SEPARATOR_RE.sub(" ", text)

    # Collapse whitespace once so affix matching sees a clean single-spaced form.
    text = _WHITESPACE_RE.sub(" ", text).strip()

    if opts.strip_affixes:
        # Deterministic: try each affix in the given order; strip head then tail once.
        for affix in opts.strip_affixes:
            norm_affix = affix
            if opts.strip_diacritics:
                norm_affix = "".join(
                    ch for ch in unicodedata.normalize("NFKD", norm_affix)
                    if not unicodedata.combining(ch)
                )
            if opts.lowercase:
                norm_affix = norm_affix.lower()
            if opts.collapse_separators:
                norm_affix = _SEPARATOR_RE.sub(" ", norm_affix)
            norm_affix = _WHITESPACE_RE.sub(" ", norm_affix).strip()
            if not norm_affix:
                continue
            if text.startswith(norm_affix):
                text = text[len(norm_affix):]
            elif text.endswith(norm_affix):
                text = text[: len(text) - len(norm_affix)]
            text = _WHITESPACE_RE.sub(" ", text).strip()

    return text


def similarity(a: str, b: str) -> float:
    """Return a [0,1] similarity ratio via difflib.SequenceMatcher (stdlib, zero dep).

    PURE. Compared on the NORMALIZED forms by the pipeline (not the raw strings) so casing
    / separators do not penalise the score; this helper itself compares its two arguments
    verbatim -- the caller passes already-normalized forms."""
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class Suggestion:
    """One proposed value mapping (PURE data; persistence is a separate step)."""

    connector: str
    source_value: str
    canonical_value: str        # the pivot value proposed as canonical
    method: str                 # 'exact' | 'normalized' | 'similarity'
    score: float                # 1.0 for exact/normalized; difflib ratio for similarity
    normalized_form: str


@dataclass
class _Item:
    """A (connector, source_value) pair with its normalized form (internal, pipeline)."""

    connector: str
    source_value: str
    normalized_form: str
    # A stable sort key so the pivot choice is deterministic:
    # (source_value, connector) -- the alphabetically-first raw value wins the pivot,
    # connector breaks ties. Documented rule (Completion Notes).
    sort_key: tuple = field(default=())


def _flatten_items(
    values_by_connector: dict[str, list[str]], opts: NormalizeOptions
) -> list[_Item]:
    """Flatten the input into a deterministically-ordered list of items (PURE).

    De-duplicates identical (connector, source_value) pairs (a connector listing a value
    twice is one item). Order: sorted by (source_value, connector) so downstream pairing
    and pivot selection are reproducible regardless of input order.
    """
    seen: set[tuple[str, str]] = set()
    items: list[_Item] = []
    for connector in sorted(values_by_connector):
        for source_value in values_by_connector[connector]:
            key = (connector, source_value)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                _Item(
                    connector=connector,
                    source_value=source_value,
                    normalized_form=normalize_value(source_value, opts),
                    sort_key=(source_value, connector),
                )
            )
    items.sort(key=lambda it: it.sort_key)
    return items


def _pivot_of(items: list[_Item]) -> str:
    """Deterministic canonical pivot for a set of equivalent items.

    Rule (documented, single choice): the RAW source_value that sorts first by
    (source_value, connector) is the pivot. Alphabetical on the raw value, connector as
    tie-break. Reproducible and independent of input order."""
    best = min(items, key=lambda it: it.sort_key)
    return best.source_value


def suggest_mappings(
    values_by_connector: dict[str, list[str]],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    normalize_options: NormalizeOptions = NormalizeOptions(),
    canonical_hint: dict[str, str] | None = None,
) -> list[Suggestion]:
    """Propose value mappings from per-connector value lists (PURE, no warehouse read).

    Three stages, safest to fuzziest (a value follows the FIRST stage that classes it):
      1. EXACT       -- raw forms identical across >= 2 connectors -> same canonical,
                        score 1.0.
      2. NORMALIZED  -- normalized forms identical (case/separators/tags/dates) but raw
                        forms differ across >= 2 connectors -> same canonical (the
                        deterministic pivot), score 1.0.
      3. SIMILARITY  -- difflib ratio of the normalized forms >= threshold -> proposal,
                        score = ratio. BELOW the threshold: NO suggestion (honest).

    ALL suggestions come out as 'proposed' at persistence time (persist_suggestions);
    NEVER auto-confirmed, even exact/normalized. canonical_hint (source_value ->
    already-known canonical) lets confirmed mappings pin the pivot: when an item's raw
    value is a hint key, its group's canonical is forced to the hint. Deterministic: same
    input -> same ordered output (stable sort of the pairs).
    """
    canonical_hint = canonical_hint or {}
    items = _flatten_items(values_by_connector, normalize_options)

    suggestions: list[Suggestion] = []
    # Items already classed by a safer stage are frozen out of the fuzzier stages
    # (priority: one stage per item, the safest -- test 14).
    classed: set[tuple[str, str]] = set()

    def _key(it: _Item) -> tuple[str, str]:
        return (it.connector, it.source_value)

    def _canonical_for(group: list[_Item]) -> str:
        for it in group:
            if it.source_value in canonical_hint:
                return canonical_hint[it.source_value]
        return _pivot_of(group)

    def _emit(group: list[_Item], method: str, score: float) -> None:
        canonical_value = _canonical_for(group)
        for it in group:
            suggestions.append(
                Suggestion(
                    connector=it.connector,
                    source_value=it.source_value,
                    canonical_value=canonical_value,
                    method=method,
                    score=score,
                    normalized_form=it.normalized_form,
                )
            )
            classed.add(_key(it))

    # -- Stage 1: EXACT -- group by RAW source_value; a group crossing >= 2 connectors.
    by_raw: dict[str, list[_Item]] = {}
    for it in items:
        by_raw.setdefault(it.source_value, []).append(it)
    for raw in sorted(by_raw):
        group = by_raw[raw]
        connectors = {it.connector for it in group}
        if len(connectors) >= 2:
            _emit(group, METHOD_EXACT, 1.0)

    # -- Stage 2: NORMALIZED -- group the still-unclassed items by normalized form; a
    # group crossing >= 2 connectors whose raw forms are NOT all identical.
    remaining = [it for it in items if _key(it) not in classed]
    by_norm: dict[str, list[_Item]] = {}
    for it in remaining:
        by_norm.setdefault(it.normalized_form, []).append(it)
    for norm in sorted(by_norm):
        group = by_norm[norm]
        if not norm:
            continue  # an empty normalized form is not a matchable identity.
        connectors = {it.connector for it in group}
        raws = {it.source_value for it in group}
        if len(connectors) >= 2 and len(raws) >= 2:
            _emit(group, METHOD_NORMALIZED, 1.0)

    # -- Stage 3: SIMILARITY -- pair the still-unclassed items across DIFFERENT
    # connectors; the best-scoring pair >= threshold anchors a proposal. Deterministic:
    # items iterated in the stable order; each unclassed item is matched to its
    # best above-threshold partner already seen.
    #
    # GREEDY PAIRWISE, NOT transitive closure: each proposal binds exactly TWO items and
    # both are then frozen (added to `classed`), so a chain A~B~C where A and C never pair
    # directly does NOT collapse into one canonical group -- the first greedy pair (A,B)
    # consumes A and B, leaving C to pair with its own next-best still-free partner (or
    # emit nothing). No connected-component / union-find merge is performed. A human
    # reconciles the residual via confirm/set_mapping_manual. (test: 3 near connectors ->
    # partial pairing, documented.)
    remaining = [it for it in items if _key(it) not in classed]
    for i in range(len(remaining)):
        left = remaining[i]
        if _key(left) in classed:
            continue
        best_j = -1
        best_score = 0.0
        for j in range(len(remaining)):
            if j == i:
                continue
            right = remaining[j]
            if _key(right) in classed:
                continue
            if right.connector == left.connector:
                continue  # only cross-connector pairs conform values.
            if not left.normalized_form or not right.normalized_form:
                continue
            score = similarity(left.normalized_form, right.normalized_form)
            # Strictly-greater keeps the first (stable-order) partner on a tie.
            if score > best_score:
                best_score = score
                best_j = j
        if best_j >= 0 and best_score >= similarity_threshold:
            group = [left, remaining[best_j]]
            _emit(group, METHOD_SIMILARITY, best_score)

    # Deterministic output order (stage priority already applied; sort for stability).
    suggestions.sort(
        key=lambda s: (s.canonical_value, s.method, s.connector, s.source_value)
    )
    return suggestions


# ---------------------------------------------------------------------------
# Row <-> dict helpers and the column list (mirrors metric_semantics.py).
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id, canonical_dimension, connector, source_value, canonical_value, status, "
    "suggestion_method, suggestion_score, normalized_form, scope_level, org_id, "
    "project_id, created_by, reviewed_by, reviewed_at, created_at, updated_at"
)

_TS_COLS = {"created_at", "updated_at", "reviewed_at"}


def _row_to_mapping(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _TS_COLS and val is not None else val
    return record


# Fields carrying no semantic change (identity/timestamps) -- excluded from the
# idempotency comparison so a re-persist with identical content emits NO 'upserted' audit.
_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _mapping_changed(before: dict | None, after: dict) -> bool:
    """True when the semantic content of a mapping changed (idempotency guard)."""
    if before is None:
        return True
    for key, value in after.items():
        if key in _VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


def _select_mapping(
    conn, *, scope_level, org_id, project_id, canonical_dimension, connector, source_value
) -> dict | None:
    """SELECT the mapping row at (scope, canonical_dimension, connector, source_value)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}
            FROM app.dimension_value_mappings
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND canonical_dimension = %s
              AND connector = %s
              AND source_value = %s
            """,
            (scope_level, org_id, project_id, canonical_dimension, connector, source_value),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_mapping(cols, row)


def _select_mapping_by_id(conn, mapping_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM app.dimension_value_mappings WHERE id = %s",
            (mapping_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_mapping(cols, row)


def _assert_no_chain(
    conn,
    *,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    canonical_dimension: str,
    source_value: str,
    canonical_value: str,
    self_id: str | None,
) -> None:
    """Refuse a confirm/manual that would create a value-mapping chain or cycle (D-3).

    Within the SAME (scope, canonical_dimension), among the CONFIRMED rows (excluding the
    row being transitioned, *self_id*), reject when:
      * ``canonical_value`` is itself a confirmed ``source_value`` (A->B while B->C exists
        -- the canonical we point at is not terminal), OR
      * ``source_value`` is a confirmed ``canonical_value`` (X->A while we confirm A->B --
        A was a terminal and would become a source; chain / cycle when it loops back).
    A no-op mapping (source_value == canonical_value, the identity) is NOT a chain.
    Raises ChainedMappingError; otherwise returns None.
    """
    if source_value == canonical_value:
        return  # identity mapping: never a chain.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_value, canonical_value
            FROM app.dimension_value_mappings
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND canonical_dimension = %s
              AND status = %s
            """,
            (scope_level, org_id, project_id, canonical_dimension, STATUS_CONFIRMED),
        )
        rows = cur.fetchall()

    for row_id, src, canon in rows:
        if self_id is not None and row_id == self_id:
            continue
        # The canonical we point at is already a source elsewhere -> A->B then B->C.
        if src == canonical_value:
            raise ChainedMappingError(
                f"mapping refuse : la valeur canonique '{canonical_value}' est deja la "
                f"source d'un autre mapping confirme (dimension '{canonical_dimension}') ; "
                "une chaine A->B->C est interdite -- pointez chaque source vers la MEME "
                "valeur canonique terminale"
            )
        # Our source is already a terminal canonical elsewhere -> X->A then A->B (cycle).
        if canon == source_value:
            raise ChainedMappingError(
                f"mapping refuse : la source '{source_value}' est deja une valeur "
                f"canonique terminale d'un autre mapping confirme (dimension "
                f"'{canonical_dimension}') ; une chaine / un cycle est interdit -- une "
                "valeur canonique ne peut pas redevenir une source"
            )


# ---------------------------------------------------------------------------
# B.4 low-level UPSERT (one mutation, one transaction, one audit) -- the core of
# persist_suggestions and set_mapping_manual.
# ---------------------------------------------------------------------------


def _upsert_mapping(
    conn,
    *,
    canonical_dimension: str,
    connector: str,
    source_value: str,
    canonical_value: str,
    status: str,
    suggestion_method: str | None,
    suggestion_score: float | None,
    normalized_form: str | None,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    identity: str,
    reviewed_by: str | None,
    reviewed_at_now: bool,
) -> tuple[dict, str]:
    """UPSERT one mapping on an EXISTING transaction (no commit). Returns (row, outcome).

    outcome is one of 'proposed'/'created', 'unchanged', 'skipped'. Never OVERWRITES an
    existing confirmed/rejected row when the incoming status is 'proposed' (a suggestion
    cannot undo a human decision) -> outcome 'skipped'. Writes ONE audit row only when
    the row actually changed. Does NOT commit (the caller owns the transaction)."""
    before = _select_mapping(
        conn,
        scope_level=scope_level,
        org_id=org_id,
        project_id=project_id,
        canonical_dimension=canonical_dimension,
        connector=connector,
        source_value=source_value,
    )

    # A 'proposed' suggestion never disturbs an already-decided (confirmed/rejected) row.
    if (
        status == STATUS_PROPOSED
        and before is not None
        and before.get("status") in (STATUS_CONFIRMED, STATUS_REJECTED)
    ):
        return before, "skipped"

    reviewed_at_sql = "now()" if reviewed_at_now else "%s"
    new_id = _mint_id(_ID_PREFIX)
    params = [
        new_id,
        canonical_dimension,
        connector,
        source_value,
        canonical_value,
        status,
        suggestion_method,
        suggestion_score,
        normalized_form,
        scope_level,
        org_id,
        project_id,
        identity,
        reviewed_by,
    ]
    if not reviewed_at_now:
        params.append(None)  # reviewed_at stays NULL

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.dimension_value_mappings
                (id, canonical_dimension, connector, source_value, canonical_value,
                 status, suggestion_method, suggestion_score, normalized_form,
                 scope_level, org_id, project_id, created_by, reviewed_by, reviewed_at,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    {reviewed_at_sql}, now(), now())
            ON CONFLICT (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
                         canonical_dimension, connector, source_value)
            DO UPDATE SET
                canonical_value   = EXCLUDED.canonical_value,
                status            = EXCLUDED.status,
                suggestion_method = EXCLUDED.suggestion_method,
                suggestion_score  = EXCLUDED.suggestion_score,
                normalized_form   = EXCLUDED.normalized_form,
                reviewed_by       = EXCLUDED.reviewed_by,
                reviewed_at       = EXCLUDED.reviewed_at,
                updated_at        = now()
            RETURNING {_COLUMNS}
            """,
            params,
        )
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description]
    after = _row_to_mapping(cols, row)

    if not _mapping_changed(before, after):
        return after, "unchanged"

    _write_semantics_audit(
        conn,
        identity=identity,
        action=ACTION_CREATED if before is None else ACTION_UPSERTED,
        entity_type=ENTITY_TYPE,
        entity_id=after["id"],
        scope_level=scope_level,
        org_id=org_id,
        project_id=project_id,
        before=before,
        after=after,
    )
    return after, ("created" if before is None else "upserted")


# ---------------------------------------------------------------------------
# B.2 Persist suggestions (PROPOSED) -- audited mutation.
# ---------------------------------------------------------------------------


def persist_suggestions(
    suggestions: list[Suggestion],
    *,
    canonical_dimension: str,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    identity: str = "system",
) -> dict:
    """UPSERT the suggestions as status='proposed' rows (keyed on the scope unicity).

    NEVER overwrites an already 'confirmed'/'rejected' mapping (a suggestion cannot undo
    a human decision -> counted 'skipped'). Writes one 'created' audit per row that
    actually changed. Idempotent: a re-play with identical content writes no audit
    (counted 'unchanged'). Returns {proposed, skipped, unchanged}."""
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    counts = {"proposed": 0, "skipped": 0, "unchanged": 0}

    with get_connection() as conn:
        for suggestion in suggestions:
            _, outcome = _upsert_mapping(
                conn,
                canonical_dimension=canonical_dimension,
                connector=suggestion.connector,
                source_value=suggestion.source_value,
                canonical_value=suggestion.canonical_value,
                status=STATUS_PROPOSED,
                suggestion_method=suggestion.method,
                suggestion_score=suggestion.score,
                normalized_form=suggestion.normalized_form,
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                identity=identity,
                reviewed_by=None,
                reviewed_at_now=False,
            )
            if outcome == "skipped":
                counts["skipped"] += 1
            elif outcome == "unchanged":
                counts["unchanged"] += 1
            else:  # 'created' or 'upserted' -> a proposed row was written/changed.
                counts["proposed"] += 1
        conn.commit()

    logger.info(
        "dimension_conformance: persisted suggestions dim=%s proposed=%d skipped=%d unchanged=%d",
        canonical_dimension,
        counts["proposed"],
        counts["skipped"],
        counts["unchanged"],
    )
    return counts


# ---------------------------------------------------------------------------
# B.3 Human curation (confirm / reject / manual) -- audited mutations.
# ---------------------------------------------------------------------------


def confirm_mapping(
    mapping_id: str, *, identity: str, canonical_value: str | None = None
) -> dict:
    """Transition a mapping to status='confirmed' + audit. Returns the persisted row.

    Sets reviewed_by/reviewed_at. May CORRECT the canonical_value at the same time (the
    human edits the proposed pivot); the correction is captured in before/after.
    Raises MappingNotFound if the id is unknown. Raises ChainedMappingError (D-3) when the
    confirmation would build a chain/cycle within the same (scope, dimension). NOT
    idempotent by design: re-confirming
    an already-confirmed row RE-STAMPS reviewed_by/reviewed_at (now()) and, because those
    change, AUDITS the re-confirmation (one more append-only row). This is the honest
    behaviour -- a re-confirmation is a real human act worth recording (who re-affirmed
    the mapping, and when), not a no-op we silently swallow."""
    return _transition(
        mapping_id,
        identity=identity,
        new_status=STATUS_CONFIRMED,
        action=ACTION_CONFIRMED,
        canonical_value=canonical_value,
    )


def reject_mapping(mapping_id: str, *, identity: str) -> dict:
    """Transition a mapping to status='rejected' + audit (recorded false-friend)."""
    return _transition(
        mapping_id,
        identity=identity,
        new_status=STATUS_REJECTED,
        action=ACTION_REJECTED,
        canonical_value=None,
    )


def _transition(
    mapping_id: str,
    *,
    identity: str,
    new_status: str,
    action: str,
    canonical_value: str | None,
) -> dict:
    """Shared confirm/reject transition (read before -> UPDATE -> audit -> commit)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_mapping_by_id(conn, mapping_id)
        if before is None:
            raise MappingNotFound(f"dimension_value_mapping not found: {mapping_id!r}")

        new_canonical = (
            canonical_value if canonical_value is not None else before["canonical_value"]
        )
        # D-3: a CONFIRM must never create a value-mapping chain/cycle. (Reject/other
        # transitions do not designate a canonical, so they cannot chain.)
        if new_status == STATUS_CONFIRMED:
            _assert_no_chain(
                conn,
                scope_level=before["scope_level"],
                org_id=before["org_id"],
                project_id=before["project_id"],
                canonical_dimension=before["canonical_dimension"],
                source_value=before["source_value"],
                canonical_value=new_canonical,
                self_id=before["id"],
            )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.dimension_value_mappings
                SET status = %s,
                    canonical_value = %s,
                    reviewed_by = %s,
                    reviewed_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING {_COLUMNS}
                """,
                (new_status, new_canonical, identity, mapping_id),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_mapping(cols, row)

        if _mapping_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=identity,
                action=action,
                entity_type=ENTITY_TYPE,
                entity_id=after["id"],
                scope_level=after["scope_level"],
                org_id=after["org_id"],
                project_id=after["project_id"],
                before=before,
                after=after,
            )
        conn.commit()
    return after


def set_mapping_manual(
    *,
    canonical_dimension: str,
    connector: str,
    source_value: str,
    canonical_value: str,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    identity: str,
) -> dict:
    """Create/UPSERT a mapping directly as status='confirmed', method='manual' + audit.

    The human-authored path (no suggestion). reviewed_by/at are set to the author now.
    Raises InvalidScope on an inconsistent scope triplet."""
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)

    with get_connection() as conn:
        # D-3: a manual confirm must never create a value-mapping chain/cycle either.
        _assert_no_chain(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            canonical_dimension=canonical_dimension,
            source_value=source_value,
            canonical_value=canonical_value,
            self_id=None,
        )
        after, _ = _upsert_mapping(
            conn,
            canonical_dimension=canonical_dimension,
            connector=connector,
            source_value=source_value,
            canonical_value=canonical_value,
            status=STATUS_CONFIRMED,
            suggestion_method=METHOD_MANUAL,
            suggestion_score=None,
            normalized_form=None,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            identity=identity,
            reviewed_by=identity,
            reviewed_at_now=True,
        )
        conn.commit()
    return after


# ---------------------------------------------------------------------------
# B.4 CRUD reads + cascade resolution PROJECT > ORG > PLATFORM.
# ---------------------------------------------------------------------------


def get_mapping(mapping_id: str) -> dict | None:
    """Return the mapping row for *mapping_id*, or None."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        return _select_mapping_by_id(conn, mapping_id)


def assert_mapping_in_org(mapping_id: str, org_id: str) -> dict:
    """Return the mapping row IF it belongs to *org_id*, else raise MappingNotFound (S-2).

    404-SEMANTIC ownership check (existence-hiding, NOT a 403): a mapping of another org is
    indistinguishable from an absent id -- both raise MappingNotFound -- so a caller cannot
    probe foreign ids. Ownership is resolved from the row's own org_id; a PROJECT-scoped row
    is matched via its project's org (app.projects.org_id), so project-scoped mappings are
    covered too. Fail-closed: an unresolvable owner org raises MappingNotFound.

    REQUIREMENT (lesson IDOR 27.2 F-1): ANY surface (REST route, MCP tool) that exposes
    confirm / reject / set_manual / delete BY ID MUST call this helper FIRST with the
    caller's authorised org, before performing the mutation. The core mutation functions
    are org-agnostic by design (single source of truth); the ORG guard is the SURFACE's
    responsibility, and this helper is the one place that guard is implemented.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        row = _select_mapping_by_id(conn, mapping_id)
        if row is None:
            raise MappingNotFound(f"dimension_value_mapping not found: {mapping_id!r}")
        row_org = row.get("org_id")
        if row_org is None and row.get("project_id"):
            # PROJECT-scoped row: resolve the owning org via the project.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s",
                    (row["project_id"],),
                )
                prow = cur.fetchone()
            row_org = prow[0] if prow else None
    if row_org is None or row_org != org_id:
        # Foreign or unresolvable owner -> indistinguishable from absent (existence-hiding).
        raise MappingNotFound(f"dimension_value_mapping not found: {mapping_id!r}")
    return row


def list_mappings_by_scope(
    *,
    canonical_dimension: str,
    scope_level: str,
    org_id: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """List mappings of a dimension at a given scope (ordered by connector, source_value)."""
    from core.db import get_connection  # noqa: PLC0415

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM app.dimension_value_mappings
                WHERE canonical_dimension = %s
                  AND scope_level = %s
                  AND COALESCE(org_id, '') = COALESCE(%s, '')
                  AND COALESCE(project_id, '') = COALESCE(%s, '')
                ORDER BY connector, source_value
                """,
                (canonical_dimension, scope_level, org_id, project_id),
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_mapping(cols, row))
    return rows


def delete_mapping(mapping_id: str, *, identity: str) -> bool:
    """Delete a mapping by id + audit ('deleted', after=None). True if a row went."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_mapping_by_id(conn, mapping_id)
        if before is None:
            return False
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.dimension_value_mappings WHERE id = %s", (mapping_id,)
            )
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DELETED,
            entity_type=ENTITY_TYPE,
            entity_id=before["id"],
            scope_level=before["scope_level"],
            org_id=before["org_id"],
            project_id=before["project_id"],
            before=before,
            after=None,
        )
        conn.commit()
    return True


# --- PURE reducer (offline-testable), calqued on reduce_definitions_by_specificity ----


def _scope_rank(row: dict) -> int:
    """Specificity rank of a row's scope (PLATFORM<ORG<PROJECT). Unknown -> -1."""
    return _SCOPE_RANK.get(row.get("scope_level"), -1)


def reduce_conformance_by_specificity(rows, *, confirmed_only: bool = True) -> dict:
    """Reduce mapping rows to {(connector, source_value) -> canonical_value} (PURE).

    A funnel merge: for each (connector, source_value) pair keep the CONFIRMED row with
    the highest scope rank (PROJECT beats ORG beats PLATFORM). With confirmed_only=True
    (the honest default) non-confirmed rows are ignored entirely, so a proposed/rejected
    row never resolves a pair. Deterministic -- a strictly-greater comparison keeps the
    first-seen on equal ranks.

    CALLER CONTRACT: at most one row per (scope_level, connector, source_value) for a
    given dimension -- the unique index uq_dimension_value_mappings_scope_key holds it at
    the DB level, so this reducer does NOT re-verify uniqueness."""
    winner: dict[tuple[str, str], dict] = {}
    for row in rows:
        if confirmed_only and row.get("status") != STATUS_CONFIRMED:
            continue
        connector = row.get("connector")
        source_value = row.get("source_value")
        if connector is None or source_value is None:
            continue
        key = (connector, source_value)
        current = winner.get(key)
        if current is None or _scope_rank(row) > _scope_rank(current):
            winner[key] = row
    return {key: row["canonical_value"] for key, row in winner.items()}


# --- DB loaders + resolution ---------------------------------------------------------


def _project_org_id(project_id: str) -> str | None:
    """Read app.projects.org_id for *project_id* (None if unknown). Fail-soft."""
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                )
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        # Fail-soft: an unresolvable project falls back to PLATFORM defaults only,
        # never a crash (mirrors metric_semantics._project_org_id).
        logger.warning(
            "dimension_conformance: project org lookup failed project=%s: %s",
            project_id,
            exc,
        )
        return None


def _load_conformance_rows(
    *, canonical_dimension: str, org_id: str | None, project_id: str | None
) -> list[dict]:
    """Load PLATFORM + (org's) ORG + (project's) PROJECT rows for a dimension.

    Confirmed rows are what the reducer keeps, but we load ALL statuses and let the pure
    reducer filter (single place for the confirmed_only policy). SQL clauses are
    HARD-CODED (scope-level literals, column list); only VALUES flow in as %s params."""
    from core.db import get_connection  # noqa: PLC0415

    clauses = ["scope_level = 'PLATFORM'"]
    params: list = [canonical_dimension]  # first %s is the canonical_dimension filter
    if org_id is not None:
        clauses.append("(scope_level = 'ORG' AND org_id = %s)")
        params.append(org_id)
    if project_id is not None:
        clauses.append("(scope_level = 'PROJECT' AND project_id = %s)")
        params.append(project_id)
    scope_where = " OR ".join(clauses)
    sql = f"""
        SELECT {_COLUMNS}
        FROM app.dimension_value_mappings
        WHERE canonical_dimension = %s AND ({scope_where})
        ORDER BY scope_level, connector, source_value
    """
    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_mapping(cols, row))
    return rows


def resolve_dimension_conformance(project_id: str, canonical_dimension: str) -> dict:
    """Return {(connector, source_value) -> canonical_value} for a project + dimension.

    Only CONFIRMED mappings, resolved in cascade PROJECT > ORG > PLATFORM (most specific
    wins, pair by pair). The project's org is read from app.projects.org_id (035); an
    unknown project resolves to PLATFORM defaults only (fail-to-platform, never a crash).
    """
    org_id = _project_org_id(project_id)
    rows = _load_conformance_rows(
        canonical_dimension=canonical_dimension, org_id=org_id, project_id=project_id
    )
    return reduce_conformance_by_specificity(rows, confirmed_only=True)


def conform_value(
    org_id: str,
    dimension: str,
    connector: str,
    source_value: str,
    *,
    project_id: str | None = None,
) -> str | None:
    """THE honest resolution: the CONFIRMED canonical value for (org, dimension,
    connector, source_value), or None.

    Signature at the ORG level (Jean's decision: ORG is the anchor). Resolution scope:
      * project_id ABSENT (default) -> ORG-anchored: the most specific CONFIRMED row
        among {ORG, PLATFORM} for the exact (connector, source_value) pair. A PROJECT
        override is intentionally invisible here (the ORG anchor is the contract).
      * project_id SUPPLIED -> the FULL cascade PROJECT > ORG > PLATFORM: the PROJECT
        clause is added and the pure reducer delegates specificity (PROJECT beats ORG
        beats PLATFORM). This is the same cascade resolve_dimension_conformance runs, but
        for a single pair. Feeds 27.5, which consumes the resolved value and MUST see a
        confirmed PROJECT override when it passes a project_id.

    Contract (locked):
      confirmed -> canonical_value ; proposed -> None ; rejected -> None ;
      absent -> None ; unknown org -> fail-to-platform (PLATFORM confirmed only, else
      None). NEVER raises on an unknown value (a franc None) -- AD-9: no silent fusion of
      an un-validated value.

    S-4: ``org_id``/``project_id`` are assumed ALREADY AUTHORIZED -- this resolution has no
    guard; the calling surface must have checked org access first (see the module trust
    note)."""
    from core.db import get_connection  # noqa: PLC0415

    # Scope clauses: PLATFORM + (this org's) ORG always; PROJECT only when project_id is
    # supplied (full cascade via project_id; ORG-anchored otherwise). HARD-CODED literals;
    # only VALUES flow in as %s params. The pure reducer picks the most specific row.
    scope_clauses = ["scope_level = 'PLATFORM'", "(scope_level = 'ORG' AND org_id = %s)"]
    params: list = [dimension, connector, source_value, org_id]
    if project_id is not None:
        scope_clauses.append("(scope_level = 'PROJECT' AND project_id = %s)")
        params.append(project_id)
    scope_where = " OR ".join(scope_clauses)

    # Load the confirmed candidates for the exact pair across those scopes, fail-soft.
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM app.dimension_value_mappings
                    WHERE canonical_dimension = %s
                      AND connector = %s
                      AND source_value = %s
                      AND status = 'confirmed'
                      AND ({scope_where})
                    """,
                    params,
                )
                cols = [desc[0] for desc in cur.description]
                rows = [_row_to_mapping(cols, r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dimension_conformance: conform_value lookup failed org=%s dim=%s: %s",
            org_id,
            dimension,
            exc,
        )
        return None

    resolved = reduce_conformance_by_specificity(rows, confirmed_only=True)
    return resolved.get((connector, source_value))


# ---------------------------------------------------------------------------
# C. CLIENT-OWNED LABELS (Story 27.9) -- app.dimension_labels, migration 106.
#
# canonical_dimension is a STABLE INTERNAL IDENTIFIER: it is written into mapping
# rows, plan payloads and kept renders, so it can never be renamed on demand --
# renaming it would break the lineage. The label the user READS is a separate,
# client-owned string resolved by the SAME cascade PROJECT > ORG > PLATFORM.
#
# HONESTY: when no row covers a dimension, resolution falls back to the identifier
# and SAYS SO (label_source='fallback_identifier'). No label is ever invented.
# ---------------------------------------------------------------------------

ENTITY_TYPE_LABEL = "dimension_label"
_LABEL_ID_PREFIX = "dlb_"

# Where a resolved label came from (contract of resolve_dimension_label).
LABEL_SOURCE_CLIENT = "client"                 # a stored row won the cascade
LABEL_SOURCE_FALLBACK = "fallback_identifier"  # nothing stored -> the stable id is shown

_LABEL_COLUMNS = (
    "id, canonical_dimension, display_label, description, scope_level, org_id, "
    "project_id, created_by, created_at, updated_at"
)


def _row_to_label(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _TS_COLS and val is not None else val
    return record


def reduce_labels_by_specificity(rows) -> dict:
    """Reduce label rows to {canonical_dimension -> row} (PURE, offline-testable).

    For each dimension the row with the highest scope rank wins (PROJECT beats ORG beats
    PLATFORM). Deterministic: a strictly-greater comparison keeps the first-seen on equal
    ranks. Rows without a dimension or without a non-empty label are ignored (a blank
    label would show the user nothing -- the DB CHECK already refuses them; this reducer
    is defensive so an offline caller cannot inject one)."""
    winner: dict[str, dict] = {}
    for row in rows:
        dimension = row.get("canonical_dimension")
        label = row.get("display_label")
        if not dimension or not label or not str(label).strip():
            continue
        current = winner.get(dimension)
        if current is None or _scope_rank(row) > _scope_rank(current):
            winner[dimension] = row
    return winner


def _load_label_rows(
    *,
    org_id: str | None,
    project_id: str | None,
    canonical_dimension: str | None = None,
) -> list[dict]:
    """Load PLATFORM + (this org's) ORG + (this project's) PROJECT label rows.

    SQL clauses are HARD-CODED (scope literals, column list); only VALUES flow in as %s.
    Fail-soft: an unreachable store yields [] (the caller then falls back to identifiers).
    """
    from core.db import get_connection  # noqa: PLC0415

    clauses = ["scope_level = 'PLATFORM'"]
    params: list = []
    if org_id is not None:
        clauses.append("(scope_level = 'ORG' AND org_id = %s)")
        params.append(org_id)
    if project_id is not None:
        clauses.append("(scope_level = 'PROJECT' AND project_id = %s)")
        params.append(project_id)
    scope_where = " OR ".join(clauses)

    where_extra = ""
    if canonical_dimension is not None:
        where_extra = " AND canonical_dimension = %s"
        params.append(canonical_dimension)

    sql = f"""
        SELECT {_LABEL_COLUMNS}
        FROM app.dimension_labels
        WHERE ({scope_where}){where_extra}
        ORDER BY canonical_dimension, scope_level
    """
    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                for row in cur.fetchall():
                    rows.append(_row_to_label(cols, row))
    except Exception as exc:  # noqa: BLE001
        logger.warning("dimension_conformance: label load failed org=%s: %s", org_id, exc)
        return []
    return rows


def resolve_dimension_labels(
    *, org_id: str | None, project_id: str | None = None
) -> dict[str, dict]:
    """Return {canonical_dimension -> {display_label, description, scope_level}}.

    ONLY the dimensions that actually have a stored label. Callers that need a value for
    an unlabelled dimension use resolve_dimension_label (which declares the fallback).

    S-4: org_id/project_id are assumed ALREADY AUTHORIZED (same trust note as
    conform_value) -- the calling surface owns the org guard."""
    rows = _load_label_rows(org_id=org_id, project_id=project_id)
    winners = reduce_labels_by_specificity(rows)
    return {
        dimension: {
            "display_label": row.get("display_label"),
            "description": row.get("description"),
            "scope_level": row.get("scope_level"),
        }
        for dimension, row in winners.items()
    }


def resolve_dimension_label(
    canonical_dimension: str, *, org_id: str | None, project_id: str | None = None
) -> dict:
    """Resolve ONE dimension's readable label. Never raises, never invents.

    Returns {'canonical_dimension', 'display_label', 'description', 'scope_level',
    'label_source'} where label_source is 'client' (a stored row won the cascade) or
    'fallback_identifier' (nothing stored -- the stable identifier is shown as-is, and the
    caller can say so)."""
    rows = _load_label_rows(
        org_id=org_id, project_id=project_id, canonical_dimension=canonical_dimension
    )
    winner = reduce_labels_by_specificity(rows).get(canonical_dimension)
    if winner is None:
        return {
            "canonical_dimension": canonical_dimension,
            "display_label": canonical_dimension,
            "description": None,
            "scope_level": None,
            "label_source": LABEL_SOURCE_FALLBACK,
        }
    return {
        "canonical_dimension": canonical_dimension,
        "display_label": winner.get("display_label"),
        "description": winner.get("description"),
        "scope_level": winner.get("scope_level"),
        "label_source": LABEL_SOURCE_CLIENT,
    }


def list_dimension_labels_by_scope(
    *, scope_level: str, org_id: str | None = None, project_id: str | None = None
) -> list[dict]:
    """List the label rows stored AT one exact scope (curation surface)."""
    from core.db import get_connection  # noqa: PLC0415

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_LABEL_COLUMNS}
                FROM app.dimension_labels
                WHERE scope_level = %s
                  AND COALESCE(org_id, '') = COALESCE(%s, '')
                  AND COALESCE(project_id, '') = COALESCE(%s, '')
                ORDER BY canonical_dimension
                """,
                (scope_level, org_id, project_id),
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_label(cols, row))
    return rows


def _select_label(
    conn, *, scope_level: str, org_id: str | None, project_id: str | None,
    canonical_dimension: str,
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_LABEL_COLUMNS}
            FROM app.dimension_labels
            WHERE scope_level = %s
              AND COALESCE(org_id, '') = COALESCE(%s, '')
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND canonical_dimension = %s
            """,
            (scope_level, org_id, project_id, canonical_dimension),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_label(cols, row)


def set_dimension_label(
    *,
    canonical_dimension: str,
    display_label: str,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    identity: str,
    description: str | None = None,
) -> dict:
    """UPSERT the client label of a dimension at one scope + one audit row.

    The stable identifier is NEVER touched -- only the string the user reads. Idempotent:
    a re-write with identical content writes NO audit line. Raises InvalidScope on an
    inconsistent triplet, ValueError on a blank identifier/label (the DB CHECK mirrors it).
    """
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    if not canonical_dimension or not canonical_dimension.strip():
        raise ValueError("canonical_dimension must not be blank")
    if not display_label or not display_label.strip():
        raise ValueError("display_label must not be blank")

    with get_connection() as conn:
        before = _select_label(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            canonical_dimension=canonical_dimension,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.dimension_labels
                    (id, canonical_dimension, display_label, description, scope_level,
                     org_id, project_id, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''),
                             canonical_dimension)
                DO UPDATE SET
                    display_label = EXCLUDED.display_label,
                    description   = EXCLUDED.description,
                    updated_at    = now()
                RETURNING {_LABEL_COLUMNS}
                """,
                (
                    _mint_id(_LABEL_ID_PREFIX),
                    canonical_dimension,
                    display_label,
                    description,
                    scope_level,
                    org_id,
                    project_id,
                    identity,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_label(cols, row)

        if _mapping_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=identity,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type=ENTITY_TYPE_LABEL,
                entity_id=after["id"],
                scope_level=scope_level,
                org_id=org_id,
                project_id=project_id,
                before=before,
                after=after,
            )
        conn.commit()
    return after


def delete_dimension_label(
    *,
    canonical_dimension: str,
    scope_level: str,
    org_id: str | None,
    project_id: str | None,
    identity: str,
) -> bool:
    """Delete the label row at one exact scope + audit. True if a row went.

    Deleting an override simply re-exposes the less specific scope (or the identifier
    fallback) -- it never deletes the dimension itself."""
    from core.db import get_connection  # noqa: PLC0415

    validate_scope(scope_level, org_id, project_id)
    with get_connection() as conn:
        before = _select_label(
            conn,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            canonical_dimension=canonical_dimension,
        )
        if before is None:
            return False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.dimension_labels WHERE id = %s", (before["id"],))
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DELETED,
            entity_type=ENTITY_TYPE_LABEL,
            entity_id=before["id"],
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            before=before,
            after=None,
        )
        conn.commit()
    return True
