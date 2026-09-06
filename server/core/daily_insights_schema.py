"""Published-insight contract for Epic 35 (agentic daily insights).

Story 35.0 (spike / hard gate). This module is the SINGLE contract shared by the
research agent, the toorow server and the existing renderer. It introduces NO new
render primitive: card composition reuses the block-type vocabulary and template ids
already defined in ``server/core/cards.py`` (Epic 23 dataviz).

Two things live here:

1. ``SCHEMA_VERSION`` + ``PUBLISHED_INSIGHT_SCHEMA`` — the JSON shape of a published
   insight payload (editorial insight + analyzed period + card contract + refs).
2. ``validate_published_insight(...)`` — a PURE, fail-closed validator that enforces
   the Epic 35 §8 gate order and returns a stable ``reason_code`` on the first failure.
   No DB / warehouse calls: every server-resolved input (available metrics/dimensions,
   resolvable evidence, freshness, project access, existing slots, expected hash) is
   passed in by the caller. The persistence + MCP wiring is Stories 35.2 / 35.3.

Design note (A vs B gate, Story 35.0): ``card.mode == "template"`` is the ratified
A path (select an existing card by keys). ``card.mode == "compose"`` (B, declarative
composition) is reserved and validator-gated but DISABLED by default — it only clears
the gate if a live spike proves it materially outperforms A (Epic 35 §10). Enabling B
later needs ``allow_compose=True``, not a schema rewrite.

ASCII-only module (L-3).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# Versioning + vocabulary (kept in lockstep with server/core/cards.py)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"

# Block types are the Epic 23 dataviz primitives (cards.py). The published-insight
# schema references them; it does not define a second vocabulary.
ALLOWED_BLOCK_TYPES = frozenset(
    # "waterfall" WAS declared here and is not any more (AI-170). The comment it
    # carried said Story 41.6 had added a `waterfall` resolver to
    # `cards._BLOCK_RESOLVERS`; `git log -S'"waterfall"' -- server/core/cards.py`
    # shows 41.6 doing the opposite, and its own message says so:
    # `0bc6aded` "la carte Tax & Fees sur mesure est DEMONTEE au profit de la
    # famille waterfall". The bespoke card was dismantled, the resolver went with
    # it, and the waterfall became a GENERIC visual family of the shared render
    # runtime -- `core/visualization_families.py::_WATERFALL`, `answer_contract`,
    # and the conformance suite literally named `test_story_41_6_generic_
    # waterfall_cutover`. Nothing was lost; this line was left behind.
    #
    # Leaving it declared was not a harmless trace: this vocabulary is what an
    # agent may EMIT, so it advertised a block that no resolver can render. The
    # trace of the remaining work lives in story 41.6 and in AI-143, not in a
    # vocabulary entry that produces an unrenderable card.
    #
    # A tax waterfall as an ORDERING of rules -- which taxes apply, in which
    # direction -- is a different object from this bridge FIGURE. It belongs to
    # the "Ordered rule-set workbench" of `docs/product-architecture/governance.md`,
    # and it is not a card block.
    {"kpi_row", "line", "bar", "gauge", "donut", "funnel", "table", "comment"}
)

# Card modes: template = A (ratified), compose = B (reserved, disabled by default).
CARD_MODE_TEMPLATE = "template"
CARD_MODE_COMPOSE = "compose"
ALLOWED_CARD_MODES = frozenset({CARD_MODE_TEMPLATE, CARD_MODE_COMPOSE})

CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})

# Non-additive metrics resolve through the semantic layer, never LLM arithmetic
# (mirrors cards._NON_ADDITIVE_METRICS). A published payload must not carry a
# free-form numeric value for these — the server resolves every number.
NON_ADDITIVE_METRICS = frozenset({"average_position", "roas", "ctr", "cpa"})

# ---------------------------------------------------------------------------
# Budgets (Epic 35 §8). No overflow truncates silently: publication is refused.
# ---------------------------------------------------------------------------

MAX_INSIGHTS_PER_DAY = 3  # slots 0..2 per (project_id, insight_date)
MAX_BLOCKS_PER_CARD = 8
MAX_POINTS_PER_SERIES = 30
MAX_TABLE_ROWS = 14
MAX_AGENT_SPEC_BYTES = 64 * 1024  # the spec the agent submits
MAX_PAYLOAD_BYTES = 512 * 1024  # the final server-enriched published payload


# ---------------------------------------------------------------------------
# Evidence vocabulary (story 53.4). ONE place declares what a ref may name, and
# the schema, the compiled pattern and the universe builder all read it, so a
# kind can never be enforced in one and advertised in the other.
# ---------------------------------------------------------------------------

#: The kinds an evidence ref may name. Both are OBSERVATIONS the server measured
#: for one project over one window.
#:
#: A CARD TEMPLATE IS DELIBERATELY NOT ONE OF THEM, and removing it is the point
#: of this constant. `card:<template>` used to be a third kind, and it made the
#: requirement satisfiable by TAUTOLOGY: gate 4 below already refuses any payload
#: whose `card.template` is not in `available_templates`, and the universe was
#: built from those same ids -- so an insight that cited nothing but the template
#: it renders cleared the evidence gate while pointing at no datum at all. A
#: published claim must point at a MEASUREMENT, not at the form that displays it:
#: a template is neither "produced for this project" nor "for this period", which
#: is exactly what AC1 of story 53.4 requires of a ref.
EVIDENCE_KINDS: tuple[str, ...] = ("metric", "dimension")

#: The shape of an evidence ref, as a pattern string. Declared in
#: `PUBLISHED_INSIGHT_SCHEMA` (what the agent READS) and compiled into
#: `_EVIDENCE_REF` (what the validator ENFORCES) from this single source.
EVIDENCE_REF_PATTERN = r"^(" + "|".join(EVIDENCE_KINDS) + r"):.+$"


def evidence_universe(
    *,
    available_metrics: set[str],
    available_dimensions: set[str],
) -> set[str]:
    """The refs that RESOLVE, built from what the server measured. Pure.

    The namespace of an evidence ref belongs to this contract module, not to a
    call site: the enforcement (`_EVIDENCE_REF`), the declaration
    (`PUBLISHED_INSIGHT_SCHEMA`) and the construction (here) then cannot drift
    apart. `core.main._resolve_evidence_refs` is a thin wrapper over this.

    EMPTY IS POSSIBLE AND FAILS CLOSED, WITH NO EXCEPTION LEFT. A warehouse
    hiccup makes the caller's availability empty, so this returns an empty
    universe and every publication is refused. The previous version of this rule
    claimed exactly that while a third kind (`card:`) was fed from a catalog
    helper that falls back to the PLATFORM DEFAULTS on failure -- so two thirds
    of the universe collapsed on an outage and one third stayed full. A comment
    that swears a guard the code does not keep is worse than no comment.
    """

    return {f"metric:{name}" for name in available_metrics} | {
        f"dimension:{name}" for name in available_dimensions
    }


# ---------------------------------------------------------------------------
# Provenance of the envelope (story 53.4, AC5 / AC6)
# ---------------------------------------------------------------------------

#: WHERE THE CONFIDENCE A SURFACE READS COMES FROM. It used to be
#: `model_declared`: `insight.confidence` was chosen by the model that also wrote
#: the prose and checked for nothing but enum membership, and story 53.4 could
#: only make that visible. `proactive-assertions.md` ("Incomplete if": *a
#: confidence level is declared by the author of the claim rather than derived*)
#: asks for the derivation, and `core.insight_confidence` now performs it from
#: the rows carrying the members the insight CITED, over the insight's own
#: period. The word is a const in the schema, so a payload that still claims
#: `model_declared` no longer validates.
CONFIDENCE_PROVENANCE = "derived"

#: Fields of the payload that are model-authored PROSE. No gate reads them.
#: `insight.title` is on this list although AC6 names only the other three: it is
#: 200 characters of model prose rendered as a headline, and a provenance block
#: that omits it would understate exactly what it exists to disclose. The
#: optional ones are reported only when the payload carries them.
MODEL_AUTHORED_FIELDS: tuple[str, ...] = (
    "insight.title",
    "insight.summary",
    "insight.whyItMatters",
    "insight.recommendedAction",
    "insight.limitations",
)


def authorship_block(payload: dict) -> dict:
    """Derive the provenance envelope of one payload. Pure and deterministic.

    STORY 53.4 -- CE QUI EST ECRIT ET CE QUI EST MESURE NE SE DEVINENT PAS.
    `proactive-assertions.md:162` makes one thing mandatory for an insight
    published without a human read: "the model-authored prose is **visibly
    distinguishable** from cited server data". A surface cannot draw that
    distinction from a payload that does not carry it, and maintaining its own
    list of field names in a component is how the two drift.

    Derived, never declared: an agent that submits its own `authorship` is
    refused (`authorship_declared`), because provenance asserted by the author of
    the prose is the same defect as confidence declared by it.

    `declaredConfidence` IS THE MODEL'S OWN WORD, KEPT AND DEMOTED. It is not
    dropped -- an author's own estimate of its claim is worth recording, and
    silently deleting it would hide that the model said something -- but it is
    carried here, under a name that says who wrote it, and NOTHING reads it as
    the confidence of the insight. The reading a surface prints is
    `derivedConfidence`, which this pure function cannot compute (it needs the
    project's rows) and which `publish` attaches from
    `core.insight_confidence.derive_insight_confidence`.
    """

    insight = payload.get("insight") if isinstance(payload.get("insight"), dict) else {}
    # An empty `recommendedAction` or `limitations` is not model-authored prose:
    # naming a field the reader will never see would make the block advisory
    # rather than exact, and a surface that highlights an absent field is a
    # surface that has to guess again.
    authored = [field for field in MODEL_AUTHORED_FIELDS if insight.get(field.split(".", 1)[1])]
    declared = insight.get("confidence")
    return {
        "modelAuthored": authored,
        "confidence": CONFIDENCE_PROVENANCE,
        "declaredConfidence": str(declared) if declared else None,
        # ONE SHAPE, ALWAYS. The key exists before the measurement does, so a
        # surface never has to tell "not measured yet" from "this build forgot to
        # send it" -- and an agent that fabricates a value here fails the equality
        # check in `_schema_shape` as `authorship_declared`.
        "derivedConfidence": None,
        "evidenceRefs": list(payload.get("evidenceRefs") or []),
    }


def authorship_of(payload: dict) -> dict:
    """The provenance block ONE payload travels with, wherever it is projected.

    Two read paths project a published insight: the Daily Insights routes
    (`daily_insights_api._with_authorship`) and the Project Overview envelope
    (`project_overview._query_daily_insights`). Both hand a reader
    `insight.confidence` -- a word the MODEL declared, whose only check is enum
    membership -- and only the first one said so. The Overview projected the bare
    word and dropped the block this module exists to attach, which is exactly the
    ambiguity story 53.4 removed on the other path.

    One function, called by both, because a second copy of the rule is how one
    surface keeps it and the other quietly stops. A payload persisted WITH its own
    `authorship` is rendered as persisted; one written before story 53.4 gets the
    derived block. Nothing is fabricated either way.

    AND THAT IS WHY THE SERVER'S READING TRAVELS INSIDE THIS BLOCK. The derived
    confidence needs the project's rows, which are gone by read time; it is
    measured once at publication and persisted on the payload, so both read paths
    get it here without a second measurement -- and a payload published before
    the derivation existed carries `derivedConfidence: None`, which every surface
    draws as `unmeasurable`. An old insight is not a low-confidence one.
    """

    stored = payload.get("authorship")
    if isinstance(stored, dict):
        return stored
    return authorship_block(payload)


# ---------------------------------------------------------------------------
# JSON schema (draft-07 flavored dict; kept dependency-free / self-validated).
# ---------------------------------------------------------------------------

PUBLISHED_INSIGHT_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "toorow published daily insight",
    "type": "object",
    "additionalProperties": False,
    "required": ["schemaVersion", "slot", "insight", "period", "card", "evidenceRefs"],
    "properties": {
        "schemaVersion": {"type": "string", "const": SCHEMA_VERSION},
        "slot": {"type": "integer", "minimum": 0, "maximum": MAX_INSIGHTS_PER_DAY - 1},
        "insight": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "summary", "whyItMatters", "confidence"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
                "whyItMatters": {"type": "string", "minLength": 1, "maxLength": 1000},
                "recommendedAction": {"type": "string", "maxLength": 1000},
                "confidence": {"type": "string", "enum": sorted(CONFIDENCE_LEVELS)},
                "limitations": {"type": "array", "items": {"type": "string"}},
            },
        },
        "period": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dateFrom", "dateTo"],
            "properties": {
                "dateFrom": {"type": "string", "format": "date"},
                "dateTo": {"type": "string", "format": "date"},
                "comparison": {"type": "string"},
            },
        },
        "card": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode"],
            "properties": {
                "mode": {"type": "string", "enum": sorted(ALLOWED_CARD_MODES)},
                # A path: a template id resolved by cards.get_template(...)
                "template": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "reportRef": {"type": "string"},
                "planId": {"type": "string"},
                # B path: declarative blocks (reserved, disabled by default)
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "binding"],
                        "properties": {
                            "type": {"type": "string", "enum": sorted(ALLOWED_BLOCK_TYPES)},
                            "title": {"type": "string"},
                            "binding": {"type": "object"},
                        },
                    },
                },
            },
        },
        # STORY 53.4 -- une preuve a une FORME, sinon il n'y a rien contre quoi la
        # resoudre. `kind:value` sur DEUX genres, tous deux mesures par le
        # serveur pour CE projet et CETTE fenetre :
        #   metric:<nom>      le serveur a mesure cette metrique sur cette fenetre
        #   dimension:<nom>   ... cette dimension
        # `card:<gabarit>` etait un troisieme genre et il est RETIRE : la gate 4
        # exige deja que `card.template` soit dans `available_templates`, donc un
        # insight qui ne citait que le gabarit qu'il rend satisfaisait la gate 5
        # par tautologie -- une obligation qui ne demande rien. La preuve pointe
        # une donnee, jamais la forme qui l'affiche.
        # Le motif est verifie pour que la barriere puisse dire << forme
        # inconnue >> avant de dire << ne resout pas >> : un ref malforme est un
        # defaut d'ecriture, un ref bien forme qui ne resout pas est une
        # affirmation sans appui. Les deux se refusent, pour des raisons
        # differentes, et confondre les deux est ce qui rendait l'ancien message
        # illisible.
        "evidenceRefs": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "pattern": EVIDENCE_REF_PATTERN},
        },
        "sourceRefs": {"type": "array", "items": {"type": "string"}},
        # SERVER-WRITTEN (story 53.4, AC5/AC6). Declared here so an enriched,
        # persisted payload stays schema-valid and re-validatable; an agent that
        # submits its own is refused, since provenance asserted by the author of
        # the prose proves nothing. See `authorship_block`.
        "authorship": {
            "type": "object",
            "additionalProperties": False,
            "required": ["modelAuthored", "confidence", "evidenceRefs"],
            "properties": {
                "modelAuthored": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "const": CONFIDENCE_PROVENANCE},
                # The model's own word, kept and DEMOTED (see `authorship_block`).
                # Nothing reads it as the confidence of the insight.
                "declaredConfidence": {"type": ["string", "null"]},
                # The server's reading, attached by `publish` AFTER validation --
                # like `frozenCard`, it comes from the warehouse and cannot be
                # recomputed by the gate, so an agent that submits an
                # `authorship` carrying it is refused as `authorship_declared`
                # (the block it submits is compared for equality against the
                # derived one, and this key is not in the derived one).
                "derivedConfidence": {"type": ["object", "null"]},
                "evidenceRefs": {"type": "array", "items": {"type": "string"}},
            },
        },
        # SERVER-WRITTEN too, and for a reason the epic stated in advance
        # (`epic-35 §7`): "le payload JSONB contient l'insight editorial ET
        # l'envelope de card figee [...] l'insight est un artefact metier durable,
        # tandis que la galerie Rendus a une retention bornee". That retention is
        # 30 days (`snapshots._DEFAULT_RETENTION_DAYS`) and the lineage column is
        # `ON DELETE SET NULL` (migration 061), so without this an insight loses
        # its CARD on day 31 and keeps only the model-authored prose no gate
        # inspects -- the evidence purged, the unbacked claim surviving.
        #
        # A CARD, NOT A CHART: three parts, because that is what `get_card`
        # produces and what §6 lists. `envelope` carries the figures, their
        # provenance and the rendered comment; `widgetUri` says which widget draws
        # them, and an envelope with no declared renderer is data rather than a
        # card; `summary` is the model-channel reading -- the part an LLM
        # transmits and adds to. Sub-shapes stay free: the envelope is AD-1's
        # form, not this module's.
        "frozenCard": {
            "type": "object",
            "properties": {
                "envelope": {"type": "object"},
                "widgetUri": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
            },
        },
    },
}


#: La forme d'un ref de preuve, compilee une fois, depuis le MEME motif que le
#: schema declare : le schema le DIT a l'agent, cette constante le FAIT
#: respecter, et les deux ne peuvent plus diverger.
_EVIDENCE_REF = re.compile(EVIDENCE_REF_PATTERN)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of ``validate_published_insight``.

    ``ok`` True means every gate passed. Otherwise ``reason_code`` is the FIRST
    failing gate (stable identifier, safe to branch on) and ``message`` is a
    human-actionable explanation. ``field_path`` points at the offending location
    when meaningful.
    """

    ok: bool
    reason_code: str | None = None
    message: str | None = None
    field_path: str | None = None


def _fail(code: str, message: str, field_path: str | None = None) -> ValidationResult:
    return ValidationResult(ok=False, reason_code=code, message=message, field_path=field_path)


def enriched_payload_refusal(payload: dict) -> ValidationResult | None:
    """Refuse an ENRICHED payload that outgrew the published-payload budget.

    `validate_published_insight` has carried a `final_payload_bytes` parameter since
    35.0 and no production caller ever passed one, so the 512 KB budget of §8 was a
    gate that had never fired. It matters now: publication embeds the resolved card
    envelope, which is the one part of the payload the agent does not size.

    Refused, never truncated -- §8 is explicit that no overrun is silently cut, and a
    half-written envelope would be worse than an absent one: it would still look like
    evidence.
    """

    measured = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if measured <= MAX_PAYLOAD_BYTES:
        return None
    return _fail(
        "payload_too_large",
        f"published payload {measured}B exceeds {MAX_PAYLOAD_BYTES}B budget",
        "$",
    )


def canonical_payload_hash(payload: dict) -> str:
    """Deterministic SHA-256 of a payload (sorted keys, compact separators).

    The published artifact is self-contained and hashed so a share stays verifiable
    even if the originating run disappears (Epic 35 §7).
    """

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _shape_errors(payload: dict) -> ValidationResult | None:
    """Minimal, dependency-free structural check against PUBLISHED_INSIGHT_SCHEMA.

    Not a full JSON-Schema engine: it enforces the constraints the gate relies on
    (types, required keys, no unknown keys, enums, const version). Anything it lets
    through is caught by the semantic gates below.
    """

    if not isinstance(payload, dict):
        return _fail("schema_shape", "payload must be a JSON object", "$")

    if payload.get("schemaVersion") != SCHEMA_VERSION:
        return _fail(
            "schema_version",
            f"schemaVersion must be '{SCHEMA_VERSION}', got {payload.get('schemaVersion')!r}",
            "schemaVersion",
        )

    for key in ("slot", "insight", "period", "card"):
        if key not in payload:
            return _fail("schema_shape", f"missing required key '{key}'", key)

    allowed_top = set(PUBLISHED_INSIGHT_SCHEMA["properties"].keys())
    unknown = set(payload) - allowed_top
    if unknown:
        return _fail("schema_shape", f"unknown top-level keys: {sorted(unknown)}", "$")

    slot = payload["slot"]
    if (
        not isinstance(slot, int)
        or isinstance(slot, bool)
        or not (0 <= slot < MAX_INSIGHTS_PER_DAY)
    ):
        return _fail(
            "schema_shape", f"slot must be an integer in [0, {MAX_INSIGHTS_PER_DAY - 1}]", "slot"
        )

    insight = payload["insight"]
    if not isinstance(insight, dict):
        return _fail("schema_shape", "insight must be an object", "insight")
    for key in ("title", "summary", "whyItMatters", "confidence"):
        if not insight.get(key):
            return _fail(
                "schema_shape", f"insight.{key} is required and non-empty", f"insight.{key}"
            )
    if insight["confidence"] not in CONFIDENCE_LEVELS:
        return _fail(
            "schema_shape",
            f"insight.confidence must be one of {sorted(CONFIDENCE_LEVELS)}",
            "insight.confidence",
        )

    period = payload["period"]
    if not isinstance(period, dict) or "dateFrom" not in period or "dateTo" not in period:
        return _fail("schema_shape", "period must carry dateFrom and dateTo", "period")

    card = payload["card"]
    if not isinstance(card, dict) or "mode" not in card:
        return _fail("schema_shape", "card must carry a mode", "card")
    if card["mode"] not in ALLOWED_CARD_MODES:
        return _fail(
            "schema_shape", f"card.mode must be one of {sorted(ALLOWED_CARD_MODES)}", "card.mode"
        )

    # `authorship` is DERIVED by the server, never declared by the author of the
    # prose it describes. A payload may carry it -- that is how an already
    # published, enriched payload re-validates -- but only if it is exactly what
    # the server computes. An agent that writes its own provenance is refused for
    # the same reason a self-declared confidence proves nothing.
    if "authorship" in payload and payload["authorship"] != authorship_block(payload):
        return _fail(
            "authorship_declared",
            "authorship is written by the server, never by the agent; "
            "remove it and the server will attach the derived block",
            "authorship",
        )

    # `frozenCard` is the RESOLVED envelope, and it is the server's to write for the
    # same reason the evidence refs are: an agent that supplies the numbers its own
    # prose cites has cited itself. Unlike `authorship` it cannot be recomputed here
    # -- it comes from the warehouse -- so there is nothing to compare against and
    # the refusal is unconditional at submission. `publish` attaches it after this
    # gate has run.
    if "frozenCard" in payload:
        return _fail(
            "frozen_card_declared",
            "frozenCard is the envelope the server resolved and froze, never one the "
            "agent supplies; remove it and publication will attach it",
            "frozenCard",
        )

    return None


def validate_published_insight(
    payload: dict,
    *,
    available_metrics: set[str],
    available_dimensions: set[str],
    available_templates: set[str],
    resolvable_evidence: set[str],
    freshness_date: str | None,
    has_project_access: bool,
    existing_slots: set[int],
    expected_hash: str | None = None,
    final_payload_bytes: int | None = None,
    allow_compose: bool = False,
    allowed_block_types: frozenset[str] = ALLOWED_BLOCK_TYPES,
) -> ValidationResult:
    """Fail-closed validation of one published-insight payload.

    Enforces the Epic 35 §8 order and returns the FIRST failing gate. Pure: all
    server-resolved facts are inputs.

    Gate order (reason_code):
      1. schema_shape / schema_version / authorship_declared / *_too_large / budget_*
      2. project_scope / idempotency                              (access + slot)
      3. mode_not_enabled / primitive_not_allowed / binding_not_allowed
      4. template_unknown / metric_unavailable / dimension_unavailable
      5. evidence_missing / evidence_malformed / evidence_unresolved
      6. stale_data / period_invalid
      7. ratio_semantic
      8. hash_mismatch
    """

    # --- Gate 1a: structure + version -------------------------------------
    shape_error = _shape_errors(payload)
    if shape_error is not None:
        return shape_error

    # --- Gate 1b: size budgets (agent spec) -------------------------------
    spec_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if spec_bytes > MAX_AGENT_SPEC_BYTES:
        return _fail(
            "spec_too_large",
            f"agent spec {spec_bytes}B exceeds {MAX_AGENT_SPEC_BYTES}B budget",
            "$",
        )
    if final_payload_bytes is not None and final_payload_bytes > MAX_PAYLOAD_BYTES:
        return _fail(
            "payload_too_large",
            f"published payload {final_payload_bytes}B exceeds {MAX_PAYLOAD_BYTES}B budget",
            "$",
        )

    card = payload["card"]
    blocks = card.get("blocks") or []
    if not isinstance(blocks, list):
        return _fail("schema_shape", "card.blocks must be an array", "card.blocks")
    if len(blocks) > MAX_BLOCKS_PER_CARD:
        return _fail(
            "budget_blocks",
            f"{len(blocks)} blocks exceeds {MAX_BLOCKS_PER_CARD}/card budget",
            "card.blocks",
        )

    # --- Gate 2: project access + idempotency -----------------------------
    if not has_project_access:
        return _fail("project_scope", "identity has no access to the target project", "$")
    slot = payload["slot"]
    if slot in existing_slots:
        return _fail("idempotency", f"slot {slot} already published for this project/date", "slot")

    # --- Gate 3: mode + primitive/binding allowlist -----------------------
    mode = card["mode"]
    if mode == CARD_MODE_COMPOSE and not allow_compose:
        return _fail(
            "mode_not_enabled",
            "card.mode 'compose' (option B) is not enabled; use 'template' (option A)",
            "card.mode",
        )
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            return _fail(
                "schema_shape", f"card.blocks[{idx}] must be an object", f"card.blocks[{idx}]"
            )
        btype = block.get("type")
        if btype not in allowed_block_types:
            return _fail(
                "primitive_not_allowed",
                f"block type {btype!r} is not an allowlisted primitive",
                f"card.blocks[{idx}].type",
            )
        binding = block.get("binding")
        if not isinstance(binding, dict):
            return _fail(
                "binding_not_allowed",
                f"card.blocks[{idx}].binding must be an object",
                f"card.blocks[{idx}].binding",
            )
        # Per-block series/table budgets (no silent truncation).
        limit = binding.get("limit")
        if btype == "table" and isinstance(limit, int) and limit > MAX_TABLE_ROWS:
            return _fail(
                "budget_table_rows",
                f"table limit {limit} exceeds {MAX_TABLE_ROWS}-row budget",
                f"card.blocks[{idx}].binding.limit",
            )
        points = binding.get("points")
        if btype in ("line", "bar") and isinstance(points, int) and points > MAX_POINTS_PER_SERIES:
            return _fail(
                "budget_points",
                f"series points {points} exceeds {MAX_POINTS_PER_SERIES} budget",
                f"card.blocks[{idx}].binding.points",
            )

    # --- Gate 4: template / metric / dimension availability ---------------
    if mode == CARD_MODE_TEMPLATE:
        template = card.get("template")
        if not template:
            return _fail(
                "template_unknown", "card.mode 'template' requires a template id", "card.template"
            )
        if template not in available_templates:
            return _fail(
                "template_unknown",
                f"template {template!r} is not in the project's available templates",
                "card.template",
            )

    referenced_metrics = _collect_metrics(card)
    for metric in referenced_metrics:
        if metric != "*" and metric not in available_metrics:
            return _fail(
                "metric_unavailable",
                f"metric {metric!r} is not available for this project",
                "card",
            )
    referenced_dimensions = _collect_dimensions(card)
    for dim in referenced_dimensions:
        if dim not in available_dimensions:
            return _fail(
                "dimension_unavailable",
                f"dimension {dim!r} is not available for this project",
                "card",
            )

    # --- Gate 5: evidence refs resolve inside server data -----------------
    #
    # THE CAVEAT THIS GATE CARRIED IS CLOSED (story 53.4, CAV-07). It used to say,
    # correctly, that requiring evidence here would switch publication OFF --
    # `core.main._resolve_evidence_refs` returned an EMPTY SET unconditionally,
    # so with no refs the answer was `evidence_missing` and with any ref
    # `evidence_unresolved`. The resolver now returns the metrics, dimensions and
    # card templates the SERVER measured for this project and window, so both
    # halves land together, which is the ordering the story insists on.
    #
    # WHY MANDATORY AND NOT MERELY CHECKED. What a reader reads next to a real,
    # figure-bearing card is `summary`, `whyItMatters` and `recommendedAction` --
    # a thousand characters each of model-authored prose that no gate inspects --
    # and `insight.confidence`, DECLARED by the model (its only check is enum
    # membership). An unbacked causal claim published in that position borrows
    # the card's credibility. `overview.md:189` asks for the opposite: any
    # available signal must be persisted and EVIDENCE-BACKED.
    #
    # ET L'OBLIGATION N'EST PLUS SATISFIABLE PAR TAUTOLOGIE. `card:<gabarit>`
    # etait un genre de preuve legal, et la gate 4 ci-dessus exige DEJA que
    # `card.template` appartienne a `available_templates` : un insight qui ne
    # citait que le gabarit qu'il rend franchissait donc cette gate sans pointer
    # aucune donnee. Une obligation qu'on satisfait en se citant soi-meme ne
    # demande rien. Le genre est retire (`EVIDENCE_KINDS`), et un `card:` reste
    # refuse avec sa propre phrase plutot qu'un << malforme >> sec.
    #
    # TROIS REFUS DISTINCTS, PARCE QUE CE SONT TROIS FAUTES DIFFERENTES. Le
    # schema declaratif (`PUBLISHED_INSIGHT_SCHEMA`) porte `required` et
    # `minItems: 1`, mais il est un CONTRAT LU, pas un validateur execute -- ce
    # fichier verifie ses formes a la main, gate par gate. Les inscrire ici est
    # donc ce qui les rend vraies :
    #   evidence_missing     rien n'est cite -> l'insight n'est adosse a rien
    #   evidence_malformed   la citation n'a pas la forme `kind:value` -> defaut
    #                        d'ecriture, l'agent peut le corriger seul
    #   evidence_unresolved  bien forme, mais ne designe rien que le serveur ait
    #                        produit -> affirmation sans appui
    # Confondre les trois est ce qui rendait le message illisible : << ne resout
    # pas >> ne dit pas a l'agent s'il a mal ecrit ou trop affirme.
    refs = payload.get("evidenceRefs")
    if not isinstance(refs, list) or not refs:
        return _fail(
            "evidence_missing",
            "evidenceRefs must cite at least one server-measured fact "
            "(metric:<name> or dimension:<name>)",
            "evidenceRefs",
        )
    for ref in refs:
        if not isinstance(ref, str) or not _EVIDENCE_REF.match(ref):
            # A `card:` ref gets its own sentence: it was a legal kind until story
            # 53.4 and it is the mistake an agent is most likely to repeat, so
            # "malformed" alone would read as a typo rather than as the rule.
            if isinstance(ref, str) and ref.startswith("card:"):
                return _fail(
                    "evidence_malformed",
                    f"evidence ref {ref!r} names a card template, which is the FORM that "
                    "displays data, not data: gate 4 already requires the template, so "
                    "citing it proves nothing. Cite 'metric:<name>' or 'dimension:<name>'",
                    "evidenceRefs",
                )
            return _fail(
                "evidence_malformed",
                f"evidence ref {ref!r} must be 'metric:<name>' or 'dimension:<name>'",
                "evidenceRefs",
            )
        if ref not in resolvable_evidence:
            return _fail(
                "evidence_unresolved",
                f"evidence ref {ref!r} does not resolve in server data",
                "evidenceRefs",
            )

    # --- Gate 6: freshness / period vs claimed date -----------------------
    period = payload["period"]
    d_from = _iso_date(period["dateFrom"])
    d_to = _iso_date(period["dateTo"])
    if d_from is None or d_to is None:
        return _fail("period_invalid", "period.dateFrom/dateTo must be ISO dates", "period")
    if d_from > d_to:
        return _fail("period_invalid", "period.dateFrom is after period.dateTo", "period")
    fresh = _iso_date(freshness_date) if freshness_date else None
    if fresh is None:
        return _fail(
            "stale_data", "no resolved freshness date; cannot present data as current", "$"
        )
    if d_to > fresh:
        return _fail(
            "stale_data",
            f"period.dateTo {d_to.isoformat()} is beyond resolved freshness {fresh.isoformat()}",
            "period.dateTo",
        )

    # --- Gate 7: non-additive metrics must not carry free numbers ---------
    #   The model selects and explains; the server resolves every number. A payload
    #   may reference a non-additive metric by name, but must never inline its value.
    ratio_error = _reject_free_ratios(card)
    if ratio_error is not None:
        return ratio_error

    # --- Gate 8: payload hash --------------------------------------------
    if expected_hash is not None:
        actual = canonical_payload_hash(payload)
        if actual != expected_hash:
            return _fail("hash_mismatch", "payload hash does not match expected hash", "$")

    return ValidationResult(ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_metrics(card: dict) -> set[str]:
    metrics: set[str] = set()
    for m in card.get("metrics") or []:
        if isinstance(m, str):
            metrics.add(m)
    for block in card.get("blocks") or []:
        binding = block.get("binding") if isinstance(block, dict) else None
        if not isinstance(binding, dict):
            continue
        val = binding.get("metrics")
        if isinstance(val, str):
            metrics.add(val)
        elif isinstance(val, list):
            metrics.update(x for x in val if isinstance(x, str))
        for key in ("numerator", "denominator", "metric"):
            v = binding.get(key)
            if isinstance(v, str):
                metrics.add(v)
    return metrics


def _collect_dimensions(card: dict) -> set[str]:
    dims: set[str] = set()
    for block in card.get("blocks") or []:
        binding = block.get("binding") if isinstance(block, dict) else None
        if not isinstance(binding, dict):
            continue
        val = binding.get("dimensions")
        if isinstance(val, str):
            dims.add(val)
        elif isinstance(val, list):
            dims.update(x for x in val if isinstance(x, str))
    return dims


def _reject_free_ratios(card: dict) -> ValidationResult | None:
    """Reject any inline numeric value attached to a non-additive metric.

    The published payload carries an editorial claim and a card *contract*; it never
    carries resolved figures for ratio/weighted metrics — those come from the semantic
    layer at render time (Epic 35 §8 step 7).
    """

    for idx, block in enumerate(card.get("blocks") or []):
        binding = block.get("binding") if isinstance(block, dict) else None
        if not isinstance(binding, dict):
            continue
        metric = binding.get("metrics")
        names = {metric} if isinstance(metric, str) else set(metric or [])
        if names & NON_ADDITIVE_METRICS and "value" in binding:
            return _fail(
                "ratio_semantic",
                f"non-additive metric in card.blocks[{idx}] must not inline a 'value'; "
                "the semantic layer resolves it",
                f"card.blocks[{idx}].binding.value",
            )
    return None
