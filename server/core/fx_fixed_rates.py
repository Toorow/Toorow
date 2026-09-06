"""A conversion rate a person POSED -- the governed door onto the `fixed` method.

RATIFIED 2026-08-17 (amendment at the end of
``docs/product-architecture/alignment-register.md``): a user must be able to set
the conversion rate as a VALUE -- a fixed rate for a currency pair over a period.
It is a governed, user-visible method with its OWN label, and it is explicitly not
to be disguised as ``fx_method='direct'``.

WHY THIS IS NOT A BRANCH OF `ingest_rate_batch`. That function lands a PROVIDER
batch and takes an :class:`~core.money_policy.FxIngestionPolicy` -- which names a
provider, a cadence and a validation bound, and which no Project has confirmed
(measured 2026-08-07: ``app.governance_rule_sets`` is empty, 0 of 18 Projects).
Routing a posed rate through it would demand two untrue things before a person
could type a number: a provider that read nothing, and an ingestion policy for an
ingestion that is not armed. The amendment says the opposite -- ingestion is
optional and its absence must not block conversion -- so the posed path stands on
its own gate.

WHAT IT REUSES, AND WHY THAT MATTERS MORE THAN THE FUNCTION IT DOES NOT REUSE.
The STORAGE is the same and this module writes no other: ``app.fx_rate_sets`` ->
``app.fx_rate_set_versions`` -> ``app.fx_rate_observations``, with their
immutability triggers, their content hashes and their one activation rule
(:func:`core.fx_rate_sets.activate_version`). A posed rate is therefore versioned,
addressable and pinnable exactly like an ingested one, and a reader that can
reproduce one can reproduce the other.

WHAT `fixed` DISCLOSES, on every row it writes:

    version.method  = 'manual_entry'   a human act, not a batch
    version.provider= 'operator'       -> `fx_source`. No feed was read.
    version.created_by                 WHO posed it
    observation.method = 'fixed'       -> `fx_method`. Never `direct`.
    observation.derivation             the window it holds for, and the note

A PERIOD IS TWO DATES AND THEY ARE BOTH STORED, and resolution SELECTS ON THEM.
``valid_from`` / ``valid_to`` are columns of ``app.fx_rate_observations``;
:func:`core.fx_rate_sets._resolve_posed` filters
``%s BETWEEN valid_from AND valid_to``, so a posed rate is not served for a day
after its window closed, and the row that wins keeps the label ``fixed`` all the
way to the reader instead of arriving as ``carry_forward``.

THIS PARAGRAPH SAID THE OPPOSITE UNTIL 2026-08-21 -- "NOT yet honest for
resolution", naming the window as remaining design. It had been false since
migration 288, and
``docs/product-architecture/capabilities/currency-fx.md`` recorded the same item
CLOSED in its amendment of 2026-08-17: *"The two items this document previously
listed as remaining design -- the condition model, and resolution over the posed
window -- are closed here."* One repository held both answers for four days. The
window is closed; what follows is what is actually open.

WHAT IS OPEN IS THE READ PATH, AND IT IS THE WHOLE OF IT. No production code
reaches :func:`core.fx_rate_sets.resolve_rate`. Measured 2026-08-21 with
``grep -rn "resolve_rate" --include=*.py server``: two callers,
``core/money_derivation.py:235`` and
``tests/integration/test_fx_conditional_rate_postgres.py:111`` -- and
``money_derivation`` has no production caller of its own,
``core/money_provenance_columns.py:68`` importing two gap CONSTANTS from it and
nothing else. What converts a monetary figure today is the dbt staging join on
``dbt/seeds/fx_rates.csv``, in 13 module staging models. So a rate posted through
this module is stored, versioned, windowed and ranked -- and no figure in any mart
is computed from it. Measured the same day against production: ``app.fx_rate_sets``,
``app.fx_rate_set_versions`` and ``app.fx_rate_observations`` hold 0 rows across 32
Projects, so nobody has been caught by that yet. The arbitration, and the one
question it waits on, are in
``docs/product-architecture/capabilities/currency-fx.md``, section
"Arbitration, 2026-08-21 -- the read path".

A CONDITIONAL FIXED RATE ("this rate WHEN ...", the spreadsheet `IF` the amendment
also names) is delivered by migration 288, in the vocabulary that already existed
rather than a second one. ``conditions`` is the SAME shape
``app.fee_tax_rules.conditions`` has carried since migration 119 -- a JSON object
mapping a key to a non-empty list of admissible values, where ``{}`` means "holds
for every row". :data:`CONDITION_KEYS` is the subset of ``core.fee_tax_rules``'s
keys that can qualify a RATE, plus ``datastream``; ``placement_type`` and
``tax_code`` are refused by name, because they qualify a fee and not a rate.

THE SIMPLE FIXED RATE IS THE DEGENERATE CASE, not a sibling of the conditional one.
An unconditional rate is one whose ``conditions`` is ``{}``, so it needs no second
code path, no second tool and no second table -- and the precedence rule falls out
for free: the more keys a condition declares, the more specific it is, so a
conditional rate outranks the unconditional one exactly when it matches, and the
unconditional one remains the fallback. See :func:`core.fx_rate_sets.resolve_rate`
for the resolution half and for what happens when two rules tie.

WHY A POSED VERSION NOW CARRIES ITS PREDECESSOR'S RATES FORWARD. Until 2026-08-17
this function minted a version holding exactly ONE observation and activated it,
superseding the previous one -- and :func:`core.fx_rate_sets.resolve_rate` reads a
single active version. Measured on the disposable base at 287: post USD/EUR, then
post GBP/EUR, and USD/EUR resolves to ``FxGap(no_rate_for_pair)``. A Project could
hold exactly one posed pair at a time and nothing said so. That was survivable
while a rate was unconditional; it makes a conditional rate impossible, because a
conditional rate and the simple rate it refines MUST coexist for either to have a
precedence. So a posed version is now a complete SNAPSHOT of the rates in force:
every observation of the outgoing version is copied forward except the one this
post replaces, which is identified by (pair, effective_date, condition).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: The `provider` of a posed rate. It lands in `fx_source`, so it has to be true:
#: no feed was read. `seed` is the dbt CSV, a provider name is a feed, and this is
#: neither -- a person typed it.
PROVIDER_POSED = "operator"

#: The rate set every posed rate lands in when a Project has none yet.
DEFAULT_RATE_SET_LABEL = "Project rates"

#: How far a posed window may reach. Not a policy knob: it is the same bound the
#: seed's `2099-12-31` quietly took, made explicit and refusable. A rate posed to
#: the end of time is the defect this whole change is about.
MAX_WINDOW_YEARS = 10

#: The closed set of keys a posed rate's condition may name (migration 288).
#:
#: BORROWED, NOT INVENTED. `country`, `market` and `connector` are three of the five
#: `core.fee_tax_rules.CONDITION_KEYS`, with the same meaning and the same
#: `{key: [values]}` shape, so a person who has written a fee condition has already
#: written an FX one. `market` sits beside `country` for the reason fee/tax gives:
#: a market is a NAMED GROUP of countries, so a multi-country market resolves a
#: market but leaves `country` unresolved.
#:
#: `datastream` is the one addition, and it is not a new idea either: fee/tax carries
#: the same notion as a `scope_kind` column because that table HAS one. This table
#: does not, so the same question is asked in the same object.
#:
#: `placement_type` and `tax_code` are deliberately EXCLUDED and refused by name.
#: They qualify a fee; a conversion rate does not depend on where an ad ran or which
#: tax code applies. Admitting them would let a person write a condition that could
#: never change the answer -- a rule that looks governed and is inert.
CONDITION_KEYS = frozenset({"connector", "country", "market", "datastream"})

#: Keys that belong to the fee/tax vocabulary but not to this one. Named separately
#: so the refusal can say WHY rather than "unknown key": being told `tax_code` is
#: unrecognised, when it is a real key one table over, sends a person looking for a
#: typo they did not make.
CONDITION_KEYS_NOT_FOR_RATES = frozenset({"placement_type", "tax_code"})


class FixedRateRefused(ValueError):
    """A posed rate was refused. Carries a typed ``code`` so a surface can render it.

    Never a bare ``ValueError``: a caller catching bad input must not swallow
    "this currency is not legal tender" and store the number anyway.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _resolve_pair(base_currency: str, quote_currency: str) -> tuple[str, str]:
    """Both sides against the governed ISO 4217 vocabulary, or a typed refusal."""
    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    base = resolve_currency(base_currency)
    quote = resolve_currency(quote_currency)
    for label, resolved, raw in (
        ("base_currency", base, base_currency),
        ("quote_currency", quote, quote_currency),
    ):
        if resolved is None:
            raise FixedRateRefused(
                "unknown_currency",
                f"{label} {raw!r} does not resolve against the governed ISO 4217 "
                "vocabulary. Use a three-letter code that does.",
            )
        if not resolved.is_tender:
            raise FixedRateRefused(
                "not_legal_tender",
                f"{resolved.code} is a {resolved.kind} code, not legal tender; "
                "a conversion rate cannot be posed for it.",
            )
    return base.code, quote.code  # type: ignore[union-attr]


def _resolve_rate(raw: Any, base: str, quote: str) -> Decimal:
    """An exact decimal, or a typed refusal. A float never reaches storage.

    ``Decimal(str(...))`` and not ``Decimal(float)``: a binary float cannot hold a
    published quotation exactly, and two systems reading "the same rate" would
    then compute two different totals -- the defect `core.money` exists to remove.
    """
    if isinstance(raw, float):
        raise FixedRateRefused(
            "float_rate",
            "A rate must be given exactly, as a string or a decimal "
            f"(got the binary float {raw!r} for {base}/{quote}). A float cannot "
            "hold a quotation exactly.",
        )
    try:
        rate = Decimal(str(raw).strip())
    except (InvalidOperation, ArithmeticError, ValueError, AttributeError) as exc:
        raise FixedRateRefused(
            "unparsable_rate", f"{raw!r} is not a rate for {base}/{quote}."
        ) from exc
    if not rate.is_finite() or rate <= 0:
        raise FixedRateRefused(
            "non_positive_rate", f"A rate must be greater than zero (got {raw!r})."
        )
    return rate


def _resolve_window(valid_from: Any, valid_to: Any) -> tuple[date, date]:
    """The period, both ends required and ordered. No open-ended posed rate."""
    parsed: list[date] = []
    for label, raw in (("valid_from", valid_from), ("valid_to", valid_to)):
        if raw is None or str(raw).strip() == "":
            raise FixedRateRefused(
                "missing_window",
                f"{label} is required. A posed rate holds for a PERIOD; a rate with "
                "no end is the '2020 to 2099' the governed method replaces.",
            )
        if isinstance(raw, date):
            parsed.append(raw)
            continue
        try:
            parsed.append(date.fromisoformat(str(raw).strip()))
        except ValueError as exc:
            raise FixedRateRefused(
                "unparsable_date", f"{label} must be an ISO date (YYYY-MM-DD)."
            ) from exc
    start, end = parsed
    if end < start:
        raise FixedRateRefused(
            "window_inverted", "valid_to falls before valid_from."
        )
    if (end.year - start.year) > MAX_WINDOW_YEARS:
        raise FixedRateRefused(
            "window_too_long",
            f"A posed rate may not be declared for more than {MAX_WINDOW_YEARS} "
            "years. Post it again for the next period instead: a rate nobody ever "
            "revisits is the defect, not the length of the form.",
        )
    return start, end


def _resolve_conditions(value: Any) -> dict[str, list[str]]:
    """The named case this rate holds for, or a typed refusal. ``{}`` is valid.

    A ONE-FOR-ONE port of :func:`core.fee_tax_rules._validate_conditions`, keeping
    its shape, its emptiness rule and -- most importantly -- its treatment of an
    unrecognised key: *an unknown key is UNRESOLVED, never ignored*. Silently
    dropping a key a person wrote is the worst available outcome, because the rate
    then applies far more widely than they asked and every figure it touches looks
    governed.

    Values are SORTED and de-duplicated before storage. ``["FR","BE"]`` and
    ``["BE","FR"]`` are one condition, and migration 288 keys uniqueness on a digest
    of the canonical jsonb -- so without this, posting the same rule twice with the
    countries typed in a different order would mint two rival authorities that tie
    forever afterwards.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FixedRateRefused(
            "conditions_not_an_object",
            "conditions must be an object mapping a key to a list of values, "
            'for example {"country": ["FR"]}.',
        )

    misplaced = sorted(set(value) & CONDITION_KEYS_NOT_FOR_RATES)
    if misplaced:
        raise FixedRateRefused(
            "condition_key_not_for_rates",
            f"{', '.join(misplaced)} qualifies a fee, not a conversion rate, so a "
            "rate conditioned on it could never resolve differently. Condition the "
            "fee rule instead; a rate admits "
            f"{', '.join(sorted(CONDITION_KEYS))}.",
        )
    unknown = sorted(set(value) - CONDITION_KEYS)
    if unknown:
        raise FixedRateRefused(
            "unknown_condition_key",
            f"conditions names {', '.join(unknown)}, which is not a condition a rate "
            "can be resolved against (an unknown key is UNRESOLVED, never ignored). "
            f"Use one of {', '.join(sorted(CONDITION_KEYS))}.",
        )

    out: dict[str, list[str]] = {}
    for key in sorted(value):
        values = value[key]
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise FixedRateRefused(
                "condition_value_not_a_list",
                f"conditions.{key} must be a list of values, even when there is only "
                f'one -- write {{"{key}": ["..."]}}.',
            )
        cleaned = {str(item).strip() for item in values if str(item).strip()}
        if not cleaned:
            raise FixedRateRefused(
                "empty_condition_value",
                f"conditions.{key} is empty. A key that admits no value matches "
                "nothing, so the rate could never apply; remove the key instead.",
            )
        out[key] = sorted(cleaned)
    return out


def _specificity(conditions: Mapping[str, Any]) -> int:
    """How specific a posed rule is: the number of keys its condition declares.

    THE COUNT IS THE RANK, and it is the honest one HERE even though fee/tax ranks by
    `scope_kind` instead. That table has a scope COLUMN (`datastream` > `plan_version`
    > `project`) and this one does not: every FX qualifier arrives in the same object,
    so the object's size is the only ordering the data actually carries. The
    unconditional rate scores 0, which is what makes it the fallback rather than a
    rival -- see :func:`core.fx_rate_sets.resolve_rate`.
    """
    return len(conditions)


#: The scale of `app.fx_rate_observations.rate` -- NUMERIC(38, 18), migration 144.
_RATE_SCALE = Decimal(1).scaleb(-18)


def _canonical_rate_text(value: Any) -> str:
    """The rate as STORAGE holds it, for hashing. Not for display.

    WHY THIS EXISTS. A rate is written as ``Decimal("0.95")`` and read back out of
    NUMERIC(38, 18) as ``Decimal("0.950000000000000000")``. Hashing ``str()`` of each
    gives two different digests for one number, so once a rate had been carried
    forward into a later version, re-posting it identically stopped being recognised
    as a replay and minted a rival version instead -- the exact duplication the
    content hash exists to prevent. Quantizing to the column's own scale makes the
    written form and the read-back form the same string.

    Display is unaffected: the caller is still handed the rate it typed.
    """
    return format(Decimal(str(value)).quantize(_RATE_SCALE), "f")


def _rates_in_force(cur, *, rate_set_id: str, replacing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The POSED rates of the outgoing version, minus the one being replaced.

    A posed version is a snapshot, so each post has to copy forward what is still in
    force. Three things this deliberately does NOT do:

    * it does not carry INGESTED observations forward. ``method = 'fixed'`` is the
      filter. An ingested batch is landed by :func:`core.fx_rate_sets.ingest_rate_batch`
      and belongs to whichever version that engine wrote; copying a provider's
      quotation into a version whose provider is ``operator`` would restate a
      measured number as a posed one, which is the exact lie migration 279 removed.
    * it does not carry a rate whose window has CLOSED. Re-posting an expired rate
      into every subsequent version would make it immortal by bookkeeping, and the
      whole point of the required window is that a rate stops.
    * it does not resolve conflicts. Two rates with the same key were already refused
      at write time by ``uq_fx_rate_observation``; ranking happens at read.

    "The one being replaced" is identified by (pair, effective_date, condition) --
    the same key migration 288 made unique. So re-posting USD/EUR under
    ``{"country":["FR"]}`` replaces THAT rule and leaves the unconditional USD/EUR
    standing, which is what a person editing one line of a table means.
    """
    from core.governance_rule_sets import canonical_json  # noqa: PLC0415

    cur.execute(
        """
        SELECT o.base_currency, o.quote_currency, o.rate, o.effective_date,
               o.valid_from, o.valid_to, o.conditions, o.derivation,
               o.source_contributors
        FROM app.fx_rate_observations o
        JOIN app.fx_rate_sets s
          ON o.rate_set_version_id = COALESCE(s.current_version_id,
                                              s.last_known_good_version_id)
        WHERE s.id = %s
          AND o.method = 'fixed'
          AND o.validation_status = 'validated'
        """,
        (rate_set_id,),
    )
    rows = cur.fetchall()

    replaced_key = (
        replacing["base_currency"],
        replacing["quote_currency"],
        replacing["effective_date"],
        canonical_json(replacing["conditions"]),
    )
    carried: list[dict[str, Any]] = []
    for row in rows:
        conditions = dict(row[6] or {})
        if (row[0], row[1], row[3], canonical_json(conditions)) == replaced_key:
            continue
        if row[5] is not None and row[5] < replacing["effective_date"]:
            # Its window closed before the day this post speaks for.
            continue
        carried.append(
            {
                "base_currency": row[0],
                "quote_currency": row[1],
                "rate": row[2],
                "effective_date": row[3],
                "valid_from": row[4],
                "valid_to": row[5],
                "conditions": conditions,
                "derivation": dict(row[7] or {}),
                "contributors": list(row[8] or []),
            }
        )
    return carried


def post_fixed_rate(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    base_currency: str,
    quote_currency: str,
    rate: Any,
    valid_from: Any,
    valid_to: Any,
    note: str = "",
    conditions: Any = None,
) -> dict[str, Any]:
    """Post (or re-post) a fixed rate for one pair over one period.

    ``conditions`` names the case the rate holds for -- ``{"country": ["FR"]}`` and
    the like. Omitted or ``{}``, the rate holds unconditionally, which is the
    ordinary fixed rate and the behaviour that shipped in 8748e279.

    Returns the stored version. Idempotent by content: posting the identical rate
    for the identical window under the identical condition returns the version
    already stored rather than minting a rival one, so a retried call cannot produce
    two authorities for one number.

    The caller owns the transaction -- this function never commits. The door does.
    """
    from ulid import ULID  # noqa: PLC0415

    from core.fx_rate_sets import (  # noqa: PLC0415
        METHOD_FIXED,
        STATUS_VALIDATED,
        activate_version,
        ensure_rate_set,
    )
    from core.governance_rule_sets import canonical_json, content_hash  # noqa: PLC0415

    base, quote = _resolve_pair(base_currency, quote_currency)
    amount = _resolve_rate(rate, base, quote)
    start, end = _resolve_window(valid_from, valid_to)
    case = _resolve_conditions(conditions)

    if base == quote and amount != Decimal(1):
        # `identity` is the same-currency EVIDENCE and it is worth exactly one.
        # A posed EUR/EUR at 1.02 is not a rate, it is a mistake with a shape.
        raise FixedRateRefused(
            "identity_rate_must_be_one",
            f"{base}/{quote} is the same currency: its rate is 1 by definition.",
        )

    actor = str(actor or "").strip()
    if not actor:
        raise FixedRateRefused("missing_actor", "A posed rate records who posed it.")

    stamp = datetime.now(timezone.utc)
    derivation = {
        "posed": True,
        "valid_from": start.isoformat(),
        "valid_to": end.isoformat(),
        "posed_by": actor,
        "note": str(note or "").strip(),
        # Kept in the derivation as well as in the column, because `derivation` is
        # what a reader of the evidence payload sees without joining anything. The
        # COLUMN is the authority resolution reads; this is the disclosure.
        "conditions": dict(case),
    }

    rate_set = ensure_rate_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        label=DEFAULT_RATE_SET_LABEL,
        actor=actor,
    )
    rate_set_id = str(rate_set["id"])

    posted = {
        "base_currency": base,
        "quote_currency": quote,
        "rate": amount,
        "effective_date": start,
        "valid_from": start,
        "valid_to": end,
        "conditions": case,
        "derivation": derivation,
        "contributors": [actor],
    }

    with conn.cursor() as cur:
        # The rates already in force, minus the one this post replaces. A posed
        # version is a SNAPSHOT (see the module header): without this, posting a
        # second pair silently removed the first from resolution, which is measured
        # and was the state until 2026-08-17.
        carried = _rates_in_force(cur, rate_set_id=rate_set_id, replacing=posted)
        snapshot = sorted(
            [*carried, posted],
            key=lambda row: (
                row["base_currency"],
                row["quote_currency"],
                row["effective_date"],
                canonical_json(row["conditions"]),
            ),
        )

        # HASHED OVER THE WHOLE SNAPSHOT, not over the rate just posted. Hashing the
        # single rate would replay wrongly: post A, post B, post A again would match
        # version 1's hash and return a version that no longer holds B, so the caller
        # would be handed an id whose contents are not what is in force.
        digest = content_hash(
            {
                "rate_set_id": rate_set_id,
                "posed": True,
                "rates": [
                    {
                        "base": row["base_currency"],
                        "quote": row["quote_currency"],
                        "rate": _canonical_rate_text(row["rate"]),
                        "effective_date": row["effective_date"].isoformat(),
                        "valid_from": row["valid_from"].isoformat()
                        if row["valid_from"]
                        else None,
                        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                        "conditions": row["conditions"],
                    }
                    for row in snapshot
                ],
            }
        )

        cur.execute(
            "SELECT id, status, as_of_date, observation_count, content_hash "
            "FROM app.fx_rate_set_versions WHERE rate_set_id = %s AND content_hash = %s",
            (rate_set_id, digest),
        )
        existing = cur.fetchone()
        if existing is not None:
            return {
                "id": existing[0],
                "rate_set_id": rate_set_id,
                "status": existing[1],
                "as_of_date": existing[2].isoformat() if existing[2] else None,
                "observation_count": existing[3],
                "content_hash": existing[4],
                "method": METHOD_FIXED,
                "base_currency": base,
                "quote_currency": quote,
                "rate": str(amount),
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat(),
                "conditions": case,
                "posed_by": actor,
                "replayed": True,
            }

        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.fx_rate_set_versions "
            "WHERE rate_set_id = %s",
            (rate_set_id,),
        )
        version_number = int(cur.fetchone()[0])
        version_id = f"fxsv_{ULID()}"
        validation = {
            "accepted": len(snapshot),
            "rejected": [],
            "posed_by": actor,
            "posted": {
                "pair": f"{base}/{quote}",
                "conditions": case,
                "window": {"valid_from": start.isoformat(), "valid_to": end.isoformat()},
            },
            "carried_forward": len(carried),
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
                STATUS_VALIDATED,
                start,
                stamp,
                PROVIDER_POSED,
                actor,
                # Already legal since migration 144: a human act has its own batch
                # method, and this is the first writer to use it.
                "manual_entry",
                canonical_json(validation),
                len(snapshot),
                digest,
                actor,
            ),
        )
        for row in snapshot:
            observation_hash = content_hash(
                {
                    "version": version_id,
                    "base": row["base_currency"],
                    "quote": row["quote_currency"],
                    "rate": _canonical_rate_text(row["rate"]),
                    "effective_date": row["effective_date"].isoformat(),
                    "method": METHOD_FIXED,
                    "conditions": row["conditions"],
                }
            )
            cur.execute(
                """
                INSERT INTO app.fx_rate_observations
                    (id, rate_set_version_id, project_id, base_currency, quote_currency,
                     rate, effective_date, as_of_date, method, derivation,
                     source_contributors, retrieved_at, validation_status, content_hash,
                     conditions, valid_from, valid_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                        %s, %s::jsonb, %s, %s)
                """,
                (
                    f"fxo_{ULID()}",
                    version_id,
                    project_id,
                    row["base_currency"],
                    row["quote_currency"],
                    row["rate"],
                    row["effective_date"],
                    row["effective_date"],
                    METHOD_FIXED,
                    canonical_json(row["derivation"]),
                    canonical_json(row["contributors"]),
                    stamp,
                    "validated",
                    observation_hash,
                    canonical_json(row["conditions"]),
                    row["valid_from"],
                    row["valid_to"],
                ),
            )
        activate_version(
            cur, project_id=project_id, rate_set_id=rate_set_id, version_id=version_id
        )

    logger.info(
        "fx_fixed_rates: posted %s/%s for project=%s window=%s..%s conditions=%s "
        "(carried %d rate(s) forward)",
        base,
        quote,
        project_id,
        start,
        end,
        sorted(case) or "none",
        len(carried),
    )
    return {
        "id": version_id,
        "rate_set_id": rate_set_id,
        "status": STATUS_VALIDATED,
        "as_of_date": start.isoformat(),
        "observation_count": len(snapshot),
        "content_hash": digest,
        "method": METHOD_FIXED,
        "base_currency": base,
        "quote_currency": quote,
        "rate": str(amount),
        "valid_from": start.isoformat(),
        "valid_to": end.isoformat(),
        "conditions": case,
        "specificity": _specificity(case),
        "carried_forward": len(carried),
        "posed_by": actor,
        "replayed": False,
    }
