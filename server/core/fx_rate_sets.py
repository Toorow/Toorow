"""Immutable FX Rate Sets, ingested asynchronously, resolved off the query path.

Story 48.3 AC4. What this replaces, exactly:

* ``dbt/seeds/fx_rates.csv`` as runtime authority -- a CSV anyone could edit,
  with no version, no provenance and no way to notice a change;
* ``fx_helper``'s synchronous provider read, which put a third-party HTTP call on
  the path of a Result, an MCP answer and an agent request;
* ``float`` rates. A published rate is a decimal quotation; a binary float cannot
  hold it exactly, so two systems reading "the same rate" compute two different
  totals.

The shape that follows from those three:

* **Ingestion writes; reads only read.** :func:`ingest_rate_batch` lands a
  provider batch. :func:`resolve_rate` opens no socket -- it reads persisted,
  validated observations and nothing else. There is no code path from a read to
  a provider, which is a structural guarantee rather than a convention.
* **Every derivation is named.** ``fixed``, ``direct``, ``triangulated``,
  ``carry_forward`` and ``identity`` are distinguishable in the returned evidence
  and in the stored row, so "fallback or triangulated rates are not identified"
  cannot recur. ``identity`` is EVIDENCE that both sides were the same currency;
  it is not a ``COALESCE(rate, 1.0)`` that also fires when the rate is simply
  missing. ``fixed`` is a rate a person POSED -- see below.

AUTOMATIC INGESTION IS NOT ARMED, AND THAT IS A RATIFIED STATE, NOT AN OMISSION.
:func:`ingest_rate_batch` was written "called by a scheduled job" and that job has
never existed: measured 2026-08-17, it has ZERO callers outside this module's own
door, and :mod:`core.fx_frankfurter`, the live provider behind it, is reached only
by its tests. The amendment of 2026-08-17 to
``docs/product-architecture/alignment-register.md`` settles what that means: rate
ingestion is **optional** and may be armed later; its absence must not block a
conversion. The posed path -- :mod:`core.fx_fixed_rates` writes a rate a person
set, through this module's storage, under the ``fixed`` method, needing no provider
and no ingestion policy -- is the one the amendment chose. Nothing here is deleted:
arming ingestion is a decision, and the code that would serve it is kept
deliberately rather than left as a surprise.

THE POSED PATH IS ARMED FOR WRITING ONLY, AND THIS DOCSTRING CLAIMED OTHERWISE
UNTIL 2026-08-21 -- it read "the path that IS armed is the posed one". A door is
armed when a PRODUCTION caller reads what it wrote. Measured 2026-08-21,
``grep -rn "resolve_rate" --include=*.py server`` gives :func:`resolve_rate`
exactly two callers -- ``core/money_derivation.py:235`` and one integration test --
and ``money_derivation`` has no production caller either. The two production
importers of this module take :func:`rate_freshness` and nothing else
(``core/capability_compilers.py:367``, ``core/project_capabilities_mcp.py:199``),
and that function reports how old a batch is; it converts nothing. So the MCP tool
``set_fixed_fx_rate`` writes into a store no monetary figure is computed from, and
:func:`rate_freshness` will report that store as ``current`` while it is -- which
is worse than a silent door, because the product then CONFIRMS a rate is in force
that governs no figure. Held to the arbitration in
``docs/product-architecture/capabilities/currency-fx.md``, section
"Arbitration, 2026-08-21 -- the read path".

Two defects must be repaired before either path serves a figure, both recorded by
the audit of 2026-08-17 and both still open on 2026-08-21:
``money_derivation.derive`` dates an undated amount with ``date.today()`` (a silent
as-of), and nothing mirrors ``app.fx_rate_observations`` to the warehouse -- the
relation is in neither ``_DEFAULT_TABLES`` nor ``_ALLOWED_TABLES`` of
:mod:`core.mirror_sync` -- so the staging join still reads
``dbt/seeds/fx_rates.csv``, in 13 module staging models.
* **Staleness refuses.** A batch older than the confirmed policy's
  ``max_staleness_days`` does not quietly serve yesterday's number: the affected
  conversion is blocked with a typed gap and a visible reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash
from core.money_policy import FxIngestionPolicy, MoneyPolicy

logger = logging.getLogger(__name__)

#: A rate a person POSED for a pair and a period -- ratified 2026-08-17 as a
#: first-class method, not a stopgap. No provider was read and none is claimed;
#: its provenance is the version above it (``manual_entry``, ``created_by``) and
#: the window in its ``derivation``. It exists so a posed number never has to
#: borrow ``direct``, which asserts an observation it never had. Written by
#: :mod:`core.fx_fixed_rates`; admitted by the CHECK since migration 279.
METHOD_FIXED = "fixed"

METHOD_DIRECT = "direct"
METHOD_TRIANGULATED = "triangulated"
METHOD_CARRY_FORWARD = "carry_forward"
METHOD_IDENTITY = "identity"

STATUS_INGESTING = "ingesting"
STATUS_VALIDATED = "validated"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"


class FxGap(RuntimeError):
    """No governed rate evidence supports this conversion. Fail closed.

    Carries the pair, the date and a typed ``code`` so a surface renders an
    honest refusal with a repair route. It is never caught into a parity.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        base: str,
        quote: str,
        on_date: date | None = None,
    ):
        self.code = code
        self.base = base
        self.quote = quote
        self.on_date = on_date
        super().__init__(message)

    def as_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "base_currency": self.base,
            "quote_currency": self.quote,
            "on_date": self.on_date.isoformat() if self.on_date else None,
        }


@dataclass(frozen=True, slots=True)
class RateEvidence:
    """A resolved rate plus everything needed to reproduce it."""

    rate: Decimal
    base_currency: str
    quote_currency: str
    method: str
    effective_date: date
    as_of_date: date
    rate_set_id: str
    rate_set_version_id: str
    provider: str
    #: For a triangulation, the pivot and the two legs; for a carry-forward, how
    #: many days were carried. Empty for a direct or identity resolution.
    derivation: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "fx_rate": str(self.rate),
            "fx_base_currency": self.base_currency,
            "fx_quote_currency": self.quote_currency,
            "fx_method": self.method,
            "fx_effective_date": self.effective_date.isoformat(),
            "fx_as_of_date": self.as_of_date.isoformat(),
            "fx_rate_set_id": self.rate_set_id,
            "fx_rate_set_version_id": self.rate_set_version_id,
            "fx_source": self.provider,
            "fx_derivation": dict(self.derivation),
        }


# ---------------------------------------------------------------------------
# Ingestion. Called by a scheduled job, never by a read.
# ---------------------------------------------------------------------------


def ensure_rate_set(
    conn,
    *,
    org_id: str,
    project_id: str,
    label: str,
    actor: str,
    ingestion_rule_set_id: str | None = None,
) -> dict[str, Any]:
    """Return the Project's rate-set head, creating it if absent."""

    from ulid import ULID  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, org_id, project_id, label, ingestion_rule_set_id, "
            "current_version_id, pending_version_id, last_known_good_version_id "
            "FROM app.fx_rate_sets WHERE project_id = %s ORDER BY created_at LIMIT 1",
            (project_id,),
        )
        row = cur.fetchone()
        if row is not None:
            return _rate_set_dict(row)
        rate_set_id = f"fxs_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.fx_rate_sets
                (id, org_id, project_id, label, ingestion_rule_set_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, org_id, project_id, label, ingestion_rule_set_id,
                      current_version_id, pending_version_id, last_known_good_version_id
            """,
            (rate_set_id, org_id, project_id, label, ingestion_rule_set_id, actor),
        )
        return _rate_set_dict(cur.fetchone())


_RATE_SET_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "label",
    "ingestion_rule_set_id",
    "current_version_id",
    "pending_version_id",
    "last_known_good_version_id",
)


def _rate_set_dict(row: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(_RATE_SET_COLUMNS, row))


def ingest_rate_batch(
    conn,
    *,
    project_id: str,
    rate_set_id: str,
    policy: FxIngestionPolicy,
    as_of_date: date,
    quotations: Sequence[Mapping[str, Any]],
    actor: str,
    retrieved_at: datetime | None = None,
    method: str = "published_batch",
) -> dict[str, Any]:
    """Land one immutable, validated batch. Returns the stored version.

    Validation is the gate AC4 requires between a provider response and an
    activated rate: a quotation that fails it is stored ``rejected`` rather than
    dropped, so "why did today's rates not activate?" has an answer. A batch that
    passes activates automatically WHEN the confirmed policy says so -- which is
    what keeps a human from having to approve rates every business day.

    Idempotent by content: re-ingesting an identical batch returns the stored
    version instead of minting a rival one.
    """

    from ulid import ULID  # noqa: PLC0415

    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    stamp = retrieved_at or datetime.now(timezone.utc)
    normalized: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for item in quotations:
        base = resolve_currency(item.get("base_currency"))
        quote = resolve_currency(item.get("quote_currency"))
        if base is None or quote is None:
            rejections.append(
                {
                    "reason": "unknown_currency",
                    "base_currency": str(item.get("base_currency")),
                    "quote_currency": str(item.get("quote_currency")),
                }
            )
            continue
        try:
            rate = Decimal(str(item["rate"]))
        except (KeyError, ArithmeticError, ValueError):
            rejections.append(
                {"reason": "unparsable_rate", "pair": f"{base.code}/{quote.code}"}
            )
            continue
        if rate <= 0:
            rejections.append({"reason": "non_positive_rate", "pair": f"{base.code}/{quote.code}"})
            continue
        effective = item.get("effective_date") or as_of_date
        if not isinstance(effective, date):
            effective = date.fromisoformat(str(effective))
        normalized.append(
            {
                "base_currency": base.code,
                "quote_currency": quote.code,
                "rate": rate,
                "effective_date": effective,
                "method": str(item.get("method") or METHOD_DIRECT),
                "derivation": dict(item.get("derivation") or {}),
                "source_contributors": [
                    str(entry) for entry in (item.get("source_contributors") or [policy.provider])
                ],
            }
        )

    normalized.sort(key=lambda entry: (entry["base_currency"], entry["quote_currency"]))
    digest = content_hash(
        {
            "rate_set_id": rate_set_id,
            "as_of_date": as_of_date.isoformat(),
            "provider": policy.provider,
            "quotations": [
                {
                    "base": entry["base_currency"],
                    "quote": entry["quote_currency"],
                    "rate": str(entry["rate"]),
                    "effective_date": entry["effective_date"].isoformat(),
                    "method": entry["method"],
                }
                for entry in normalized
            ],
        }
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, as_of_date, observation_count, content_hash "
            "FROM app.fx_rate_set_versions WHERE rate_set_id = %s AND content_hash = %s",
            (rate_set_id, digest),
        )
        existing = cur.fetchone()
        if existing is not None:
            return {
                "id": existing[0],
                "status": existing[1],
                "as_of_date": existing[2],
                "observation_count": existing[3],
                "content_hash": existing[4],
                "replayed": True,
            }

        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.fx_rate_set_versions "
            "WHERE rate_set_id = %s",
            (rate_set_id,),
        )
        version_number = int(cur.fetchone()[0])
        version_id = f"fxsv_{ULID()}"
        status = STATUS_VALIDATED if normalized and not _fails_validation(
            normalized, policy, rejections
        ) else STATUS_REJECTED
        validation = {
            "accepted": len(normalized),
            "rejected": rejections,
            "max_relative_move": policy.max_relative_move,
            "provider": policy.provider,
        }
        cur.execute(
            """
            INSERT INTO app.fx_rate_set_versions
                (id, rate_set_id, project_id, version_number, status, as_of_date,
                 retrieved_at, provider, provider_reference, method, validation,
                 observation_count, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (
                version_id,
                rate_set_id,
                project_id,
                version_number,
                status,
                as_of_date,
                stamp,
                policy.provider,
                policy.provider_reference,
                method,
                canonical_json(validation),
                len(normalized),
                digest,
                actor,
            ),
        )
        for entry in normalized:
            observation_hash = content_hash(
                {
                    "version": version_id,
                    "base": entry["base_currency"],
                    "quote": entry["quote_currency"],
                    "rate": str(entry["rate"]),
                    "effective_date": entry["effective_date"].isoformat(),
                    "method": entry["method"],
                }
            )
            cur.execute(
                """
                INSERT INTO app.fx_rate_observations
                    (id, rate_set_version_id, project_id, base_currency, quote_currency,
                     rate, effective_date, as_of_date, method, derivation,
                     source_contributors, retrieved_at, validation_status, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                """,
                (
                    f"fxo_{ULID()}",
                    version_id,
                    project_id,
                    entry["base_currency"],
                    entry["quote_currency"],
                    entry["rate"],
                    entry["effective_date"],
                    as_of_date,
                    entry["method"],
                    canonical_json(entry["derivation"]),
                    canonical_json(entry["source_contributors"]),
                    stamp,
                    STATUS_VALIDATED if status == STATUS_VALIDATED else STATUS_REJECTED,
                    observation_hash,
                ),
            )

        if status == STATUS_VALIDATED and policy.auto_activate_validated_batches:
            _activate(cur, project_id=project_id, rate_set_id=rate_set_id, version_id=version_id)
        elif status == STATUS_VALIDATED:
            cur.execute(
                "UPDATE app.fx_rate_sets SET pending_version_id = %s, updated_at = NOW() "
                "WHERE id = %s AND project_id = %s",
                (version_id, rate_set_id, project_id),
            )

    return {
        "id": version_id,
        "status": status,
        "as_of_date": as_of_date,
        "observation_count": len(normalized),
        "content_hash": digest,
        "validation": validation,
        "replayed": False,
    }


def _fails_validation(
    normalized: Sequence[Mapping[str, Any]],
    policy: FxIngestionPolicy,
    rejections: list[dict[str, Any]],
) -> bool:
    """A batch fails when any quotation was unusable. Partial is not validated.

    Half a batch is worse than none: the pairs that landed would convert and the
    ones that did not would gap, so the same Result would mix a governed rate with
    a refusal for no reason a reader could see.
    """
    return bool(rejections)


def activate_version(cur, *, project_id: str, rate_set_id: str, version_id: str) -> None:
    """Point a Project's rate set at *version_id*, superseding the previous one.

    Public because a second writer needs it and reaching into ``_activate`` from
    another module would make the pointer's owner ambiguous:
    :mod:`core.fx_fixed_rates` lands a posed rate and must activate it the same
    way a validated batch is activated -- one activation rule, two writers.
    """
    _activate(cur, project_id=project_id, rate_set_id=rate_set_id, version_id=version_id)


def _activate(cur, *, project_id: str, rate_set_id: str, version_id: str) -> None:
    cur.execute(
        "SELECT current_version_id FROM app.fx_rate_sets WHERE id = %s AND project_id = %s",
        (rate_set_id, project_id),
    )
    row = cur.fetchone()
    previous = row[0] if row else None
    if previous and previous != version_id:
        cur.execute(
            "UPDATE app.fx_rate_set_versions SET status = 'superseded' "
            "WHERE id = %s AND status = 'validated'",
            (previous,),
        )
    cur.execute(
        """
        UPDATE app.fx_rate_sets
        SET current_version_id = %s,
            last_known_good_version_id = COALESCE(%s, %s),
            pending_version_id = NULL,
            updated_at = NOW()
        WHERE id = %s AND project_id = %s
        """,
        (version_id, previous, version_id, rate_set_id, project_id),
    )


# ---------------------------------------------------------------------------
# Resolution. Reads persisted observations. Opens no socket, ever.
# ---------------------------------------------------------------------------


def resolve_rate(
    conn,
    *,
    project_id: str,
    base_currency: str,
    quote_currency: str,
    on_date: date,
    policy: MoneyPolicy,
    context: Mapping[str, Any] | None = None,
) -> RateEvidence:
    """The governed rate for a pair on a date, or :class:`FxGap`.

    Order of resolution, and each step is recorded in the returned evidence:

      1. ``identity`` -- same currency. Exact, and stated as evidence.
      2. ``fixed`` -- a rate a person POSED whose window covers the date and whose
         condition holds for *context*. Ranked, never guessed -- see below.
      3. ``direct`` -- an observation for the pair effective on that date.
      4. ``triangulated`` -- only when the confirmed policy allows it, through
         the pivot the policy names, from two observations of the same batch.
      5. ``carry_forward`` -- only when the policy allows it, from the most
         recent earlier observation, and only inside ``max_staleness_days``.

    Anything else raises. There is no sixth step and no default.

    *context* describes the figure being converted, in the keys
    :data:`core.fx_fixed_rates.CONDITION_KEYS` names -- ``connector``, ``country``,
    ``market``, ``datastream``. It is optional because an unconditional rate needs
    none; it is required, in practice, by any Project that posts a conditional one,
    and a condition this context cannot answer produces a NAMED gap rather than a
    quiet fall-through to the general rate.

    THE POSED STEP RUNS BEFORE `direct` because a rate a person set for this Project
    is more specific than any quotation a provider published for the world. Once
    ingestion is armed the two will coexist in one rate set, and this ordering is the
    only one under which posting a rate does what the person posting it expects.
    """

    base = base_currency.strip().upper()
    quote = quote_currency.strip().upper()
    if base == quote:
        return RateEvidence(
            rate=Decimal(1),
            base_currency=base,
            quote_currency=quote,
            method=METHOD_IDENTITY,
            effective_date=on_date,
            as_of_date=on_date,
            rate_set_id="",
            rate_set_version_id="",
            provider="identity",
            derivation={"reason": "same currency on both sides"},
        )

    active = _active_rate_set_version(conn, project_id=project_id)
    if active is None:
        raise FxGap(
            "no_rate_set",
            "This Project has no validated FX Rate Set, so no cross-currency value "
            "can be derived.",
            base=base,
            quote=quote,
            on_date=on_date,
        )
    rate_set_id, version_id, provider, batch_as_of = active

    posed = _resolve_posed(
        conn,
        version_id=version_id,
        base=base,
        quote=quote,
        on_date=on_date,
        context=context,
    )
    if posed is not None:
        rate, effective, derivation = posed
        return RateEvidence(
            rate=rate,
            base_currency=base,
            quote_currency=quote,
            # KEPT `fixed` ALL THE WAY TO THE READER. Before 2026-08-17 a posed rate
            # arrived here labelled `carry_forward`, because the only path that could
            # serve it was the staleness one -- so the label migration 279 exists to
            # give it was lost at exactly the moment somebody read it.
            method=METHOD_FIXED,
            effective_date=effective,
            as_of_date=batch_as_of,
            rate_set_id=rate_set_id,
            rate_set_version_id=version_id,
            provider=provider,
            derivation=derivation,
        )

    direct = _observation(conn, version_id, base, quote, on_date)
    if direct is not None:
        rate, effective = direct
        _guard_staleness(effective, on_date, policy, base, quote)
        return RateEvidence(
            rate=rate,
            base_currency=base,
            quote_currency=quote,
            method=METHOD_DIRECT,
            effective_date=effective,
            as_of_date=batch_as_of,
            rate_set_id=rate_set_id,
            rate_set_version_id=version_id,
            provider=provider,
            derivation={},
        )

    if policy.allow_triangulation and policy.triangulation_pivot:
        pivot = policy.triangulation_pivot
        leg_one = _observation(conn, version_id, base, pivot, on_date)
        leg_two = _observation(conn, version_id, pivot, quote, on_date)
        if leg_one is not None and leg_two is not None:
            (rate_one, effective_one), (rate_two, effective_two) = leg_one, leg_two
            effective = min(effective_one, effective_two)
            _guard_staleness(effective, on_date, policy, base, quote)
            return RateEvidence(
                rate=rate_one * rate_two,
                base_currency=base,
                quote_currency=quote,
                method=METHOD_TRIANGULATED,
                effective_date=effective,
                as_of_date=batch_as_of,
                rate_set_id=rate_set_id,
                rate_set_version_id=version_id,
                provider=provider,
                derivation={
                    "pivot": pivot,
                    "legs": [
                        {"pair": f"{base}/{pivot}", "rate": str(rate_one)},
                        {"pair": f"{pivot}/{quote}", "rate": str(rate_two)},
                    ],
                },
            )

    if policy.allow_carry_forward:
        carried = _observation(conn, version_id, base, quote, on_date, allow_earlier=True)
        if carried is not None:
            rate, effective = carried
            _guard_staleness(effective, on_date, policy, base, quote)
            return RateEvidence(
                rate=rate,
                base_currency=base,
                quote_currency=quote,
                method=METHOD_CARRY_FORWARD,
                effective_date=effective,
                as_of_date=batch_as_of,
                rate_set_id=rate_set_id,
                rate_set_version_id=version_id,
                provider=provider,
                derivation={"carried_days": (on_date - effective).days},
            )

    raise FxGap(
        "no_rate_for_pair",
        f"No governed rate resolves {base} to {quote} on {on_date.isoformat()} "
        "under the confirmed Money Policy.",
        base=base,
        quote=quote,
        on_date=on_date,
    )


# ---------------------------------------------------------------------------
# The POSED step. Windows, conditions, precedence, and the refusal at a tie.
# ---------------------------------------------------------------------------

#: A condition clause the *context* cannot answer. Borrowed verbatim from the
#: three-valued logic of ``dbt/macros/fee_tax_condition_matcher.sql``: MATCH,
#: NO_MATCH, and UNRESOLVED -- where UNRESOLVED beats NO_MATCH, because "if one
#: clause cannot be evaluated, the conjunction is UNKNOWN, not false".
_MATCH, _NO_MATCH, _UNRESOLVED = 0, 1, 2


def _evaluate_condition(
    conditions: Mapping[str, Any], context: Mapping[str, Any] | None
) -> tuple[int, str | None]:
    """Three-valued verdict for one posed rule against one figure's context.

    Returns ``(verdict, unresolved_key)``. An empty condition is a MATCH by
    definition -- that is what makes the ordinary fixed rate the fallback rather
    than a special case.

    WHY UNRESOLVED IS NOT `NO_MATCH`. If a rule says "when country = FR" and the
    figure carries no country, the truthful answer is *unknown*: the rule might
    apply. Treating it as false would silently serve the general rate and produce a
    total that looks governed, which is the whole family of defect
    :mod:`core.money_derivation` exists to remove. It becomes a typed gap instead.
    """
    if not conditions:
        return _MATCH, None
    ctx = context or {}
    for key in sorted(conditions):
        if key not in ctx or ctx[key] is None or str(ctx[key]).strip() == "":
            return _UNRESOLVED, key
        admissible = {str(item).strip() for item in conditions[key]}
        if str(ctx[key]).strip() not in admissible:
            return _NO_MATCH, None
    return _MATCH, None


def _resolve_posed(
    conn,
    *,
    version_id: str,
    base: str,
    quote: str,
    on_date: date,
    context: Mapping[str, Any] | None,
) -> tuple[Decimal, date, dict[str, Any]] | None:
    """The posed rate that governs this figure, or ``None`` if no posed rule covers it.

    Candidacy is the WINDOW, not the effective date: a posed rate holds for the
    period its author declared, so it applies on any day inside
    ``[valid_from, valid_to]``. The staleness guard is deliberately NOT applied to a
    posed rate -- ``max_staleness_days`` bounds how old a QUOTATION may be before it
    stops describing the market, and a rate somebody declared for a period is not
    describing the market, it IS the agreed number for that period. Refusing it as
    "too stale" would overrule the person who set it with a policy about providers.

    PRECEDENCE: the most specific matching rule wins, specificity being the number of
    condition keys declared (:func:`core.fx_fixed_rates._specificity`). So a rule
    conditioned on country beats the unconditional rate whenever it matches, and the
    unconditional rate is the fallback.

    A TIE IS REFUSED, NOT BROKEN. Two rules of equal specificity that both match are
    a governance defect, and the refusal names both. This is the discipline
    ``core.tax_fee_rule_set.validate_ladder_rules`` already applies to the fee ladder
    -- "the composed total would depend on row order" -- and the reason is stronger
    here: a fee picked by ``rule_id ASC`` still shows up in ``applied_rule_ids``,
    whereas a RATE picked that way silently restates every monetary figure it touches
    and leaves nothing behind to notice it by.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, rate, effective_date, valid_from, valid_to, conditions
            FROM app.fx_rate_observations
            WHERE rate_set_version_id = %s
              AND base_currency = %s AND quote_currency = %s
              AND method = 'fixed'
              AND validation_status = 'validated'
              AND valid_from IS NOT NULL
              AND %s BETWEEN valid_from AND valid_to
            """,
            (version_id, base, quote, on_date),
        )
        rows = cur.fetchall()
    if not rows:
        return None

    matched: list[tuple[int, str, Decimal, date, dict[str, Any]]] = []
    unresolved: list[tuple[str, str]] = []
    for obs_id, rate, effective, _valid_from, valid_to, conditions in rows:
        case = dict(conditions or {})
        verdict, missing = _evaluate_condition(case, context)
        if verdict == _UNRESOLVED:
            unresolved.append((str(obs_id), str(missing)))
            continue
        if verdict == _NO_MATCH:
            continue
        matched.append(
            (len(case), str(obs_id), Decimal(str(rate)), effective, case)
        )

    if unresolved:
        keys = sorted({key for _, key in unresolved})
        raise FxGap(
            "fx_condition_unresolved",
            f"A posed {base}/{quote} rate holds only when "
            f"{', '.join(keys)} is known, and this figure does not carry it. Either "
            f"supply {', '.join(keys)} with the figure, or post an unconditional "
            f"{base}/{quote} rate to fall back on.",
            base=base,
            quote=quote,
            on_date=on_date,
        )
    if not matched:
        return None

    best = max(rank for rank, _, _, _, _ in matched)
    winners = [row for row in matched if row[0] == best]
    if len(winners) > 1:
        contenders = ", ".join(sorted(obs_id for _, obs_id, _, _, _ in winners))
        raise FxGap(
            "fx_condition_conflict",
            f"{len(winners)} posed {base}/{quote} rates match this figure on "
            f"{on_date.isoformat()} and none is more specific than the others "
            f"({contenders}). Which one is meant cannot be read off the rules, so no "
            "rate is served: narrow one of the conditions, or retire one of the rules.",
            base=base,
            quote=quote,
            on_date=on_date,
        )

    rank, obs_id, rate, effective, case = winners[0]
    return (
        rate,
        effective,
        {
            "posed": True,
            "observation_id": obs_id,
            # WHICH RULE, AND WHICH CONDITION MATCHED. A reader six months later must
            # be able to say why this figure converted at this number and not the one
            # in the row above it; "a fixed rate applied" does not answer that.
            "conditions": case,
            "specificity": rank,
            "matched_context": {key: str(context[key]) for key in sorted(case)}
            if case and context
            else {},
            "candidates_considered": len(matched),
        },
    )


def _guard_staleness(
    effective: date, on_date: date, policy: MoneyPolicy, base: str, quote: str
) -> None:
    age = (on_date - effective).days
    if age > policy.max_staleness_days:
        raise FxGap(
            "rate_too_stale",
            f"The newest governed {base}/{quote} rate is {age} days older than the "
            f"figure's date; the confirmed policy allows {policy.max_staleness_days}.",
            base=base,
            quote=quote,
            on_date=on_date,
        )


def _active_rate_set_version(
    conn, *, project_id: str
) -> tuple[str, str, str, date] | None:
    """The current validated version, falling back to last-known-good.

    Last-known-good is a POLICY fallback, not a repair: the staleness guard still
    applies to whatever it returns, so an old LKG blocks the conversion instead of
    quietly serving a rate from another month.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id,
                   COALESCE(s.current_version_id, s.last_known_good_version_id),
                   v.provider, v.as_of_date
            FROM app.fx_rate_sets s
            JOIN app.fx_rate_set_versions v
              ON v.id = COALESCE(s.current_version_id, s.last_known_good_version_id)
             AND v.project_id = s.project_id
            WHERE s.project_id = %s AND v.status IN ('validated', 'superseded')
            ORDER BY v.as_of_date DESC
            LIMIT 1
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), str(row[2]), row[3]


def _observation(
    conn,
    version_id: str,
    base: str,
    quote: str,
    on_date: date,
    *,
    allow_earlier: bool = False,
) -> tuple[Decimal, date] | None:
    """An INGESTED observation for the pair. Posed rates are excluded on purpose.

    ``method <> 'fixed'`` is what stops the `direct`, `triangulated` and
    `carry_forward` steps from picking up a rate a person posed and serving it under
    a label that asserts an observation it never had. Measured before this line
    existed: a rate posed for 2026 resolved on 2027-06-01 -- outside its own window
    -- reported as ``carry_forward``. The posed step above owns those rows, honours
    their window, and calls them ``fixed``.
    """
    comparison = "<=" if allow_earlier else "="
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT rate, effective_date
            FROM app.fx_rate_observations
            WHERE rate_set_version_id = %s AND base_currency = %s AND quote_currency = %s
              AND validation_status = 'validated' AND effective_date {comparison} %s
              AND method <> 'fixed'
            ORDER BY effective_date DESC
            LIMIT 1
            """,
            (version_id, base, quote, on_date),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Decimal(str(row[0])), row[1]


def rate_freshness(conn, *, project_id: str) -> dict[str, Any]:
    """A bounded summary for Settings and MCP: which batch is live, and how old.

    Deliberately not a rate list. AC10 keeps large rate collections app-only
    behind a bounded handle; this is the summary a card and a model summary share.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, v.id, v.status, v.as_of_date, v.retrieved_at, v.provider,
                   v.observation_count,
                   (s.current_version_id IS NULL) AS on_last_known_good
            FROM app.fx_rate_sets s
            LEFT JOIN app.fx_rate_set_versions v
              ON v.id = COALESCE(s.current_version_id, s.last_known_good_version_id)
            WHERE s.project_id = %s
            ORDER BY s.created_at
            LIMIT 1
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or row[1] is None:
        return {"state": "no_rate_set", "rate_set_id": row[0] if row else None}
    return {
        "state": "on_last_known_good" if row[7] else "current",
        "rate_set_id": row[0],
        "rate_set_version_id": row[1],
        "status": row[2],
        "as_of_date": row[3].isoformat() if row[3] else None,
        "retrieved_at": row[4].isoformat() if row[4] else None,
        "provider": row[5],
        "observation_count": row[6],
    }
