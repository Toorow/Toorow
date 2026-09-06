"""Single decision point for how a Project default earned the value it carries.

Why this module exists (AI-77 / AI-81, review-epic-48.md C-3)
------------------------------------------------------------
Four separate paths created a Project and all four wrote the same literal into
`app.project_preferences`::

    canonical_currency_origin = 'suggestion'
    reporting_timezone_origin = 'suggestion'

while the values themselves came from ``body.get("currency") or "EUR"`` and
``body.get("timezone") or "Europe/Paris"``. Nothing suggested EUR. The row
claimed a provenance it had not earned -- the same class as a
``verification: blocked`` that claims a state nobody won.

The repair is not to pick a better constant. It is to make the origin a
*decision* with exactly one place to read it, so a value can never be labelled
as suggested unless something actually suggested it.

Three origins, and the evidence each one requires
-------------------------------------------------
``operator``
    A human sent the value on the request. The evidence is the request itself.

``suggestion``
    A connected source told us. This origin REQUIRES a non-empty ``evidence``
    string naming what said so -- :func:`decide` refuses it otherwise, which is
    what stops the old defect from being reintroduced by a later caller.

``default``
    Nobody said anything and the column is ``NOT NULL``, so the platform
    fallback is written and *named as a fallback*. The screen can then say
    "Default: EUR (nothing has suggested a currency yet)" instead of showing a
    fabricated suggestion.

On deriving a real suggestion
-----------------------------
`capabilities/currency-fx.md` gives the correct order: native amount and
currency, then the *choice* of reporting currency, then dated FX. So a source
currency is extracted and only the restitution currency is chosen. Deriving a
genuine suggestion therefore needs somewhere to read a connected account's
declared currency from -- and that store does not exist yet: no table under the
`app` schema holds a source account currency (checked against
`information_schema.columns` on 2026-07-31). :func:`derive_from_connected_sources`
records that absence honestly rather than inventing a value, and returns the
reason so a caller can show it.
"""

from __future__ import annotations

from dataclasses import dataclass

ORIGIN_OPERATOR = "operator"
ORIGIN_SUGGESTION = "suggestion"
ORIGIN_DEFAULT = "default"

VALID_ORIGINS = (ORIGIN_OPERATOR, ORIGIN_SUGGESTION, ORIGIN_DEFAULT)

# The column is NOT NULL (migration 008) and migration 144 dropped its DEFAULT,
# so a row must carry something. These are the platform fallbacks, and the whole
# point of this module is that they travel labelled `default`, never `suggestion`.
PLATFORM_FALLBACK_CURRENCY = "EUR"
PLATFORM_FALLBACK_TIMEZONE = "Europe/Paris"

CONFIRMATION_UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class PreferenceDecision:
    """A Project default plus how it got there.

    `evidence` is what a screen shows next to the value. It is mandatory for
    `suggestion` and must stay ``None`` for the other two origins: an operator
    value is evidenced by the request, and a fallback has no evidence by
    definition -- that is what makes it a fallback.
    """

    value: str
    origin: str
    evidence: str | None = None

    @property
    def confirmation_status(self) -> str:
        """Nothing written at creation time is confirmed, whatever its origin.

        Confirmation is an act performed against a Project Configuration
        Version; it is never a side effect of insertion.
        """
        return CONFIRMATION_UNCONFIRMED


def decide(
    requested: str | None,
    *,
    fallback: str,
    suggestion: str | None = None,
    suggestion_evidence: str | None = None,
) -> PreferenceDecision:
    """Resolve one Project default and the origin it actually earned.

    Precedence is operator > suggestion > platform fallback: an explicit human
    value is never overridden by a guess.

    Raises:
        ValueError: if a suggestion is offered without naming what suggested it.
            This is deliberate -- it is the guard that stops C-3 coming back.
    """
    if suggestion is not None and not (suggestion_evidence or "").strip():
        raise ValueError(
            "a suggested Project default must name its source; "
            "pass suggestion_evidence, or do not claim a suggestion"
        )

    cleaned = (requested or "").strip()
    if cleaned:
        return PreferenceDecision(value=cleaned, origin=ORIGIN_OPERATOR)

    if suggestion is not None:
        return PreferenceDecision(
            value=suggestion.strip(),
            origin=ORIGIN_SUGGESTION,
            evidence=(suggestion_evidence or "").strip(),
        )

    return PreferenceDecision(value=fallback, origin=ORIGIN_DEFAULT)


def decide_currency(
    requested: str | None,
    *,
    suggestion: str | None = None,
    suggestion_evidence: str | None = None,
) -> PreferenceDecision:
    """Resolve the Project reporting currency, upper-cased as ISO-4217 is."""
    decision = decide(
        requested,
        fallback=PLATFORM_FALLBACK_CURRENCY,
        suggestion=suggestion,
        suggestion_evidence=suggestion_evidence,
    )
    return PreferenceDecision(
        value=decision.value.upper(),
        origin=decision.origin,
        evidence=decision.evidence,
    )


def decide_timezone(
    requested: str | None,
    *,
    suggestion: str | None = None,
    suggestion_evidence: str | None = None,
) -> PreferenceDecision:
    """Resolve the Project reporting timezone."""
    return decide(
        requested,
        fallback=PLATFORM_FALLBACK_TIMEZONE,
        suggestion=suggestion,
        suggestion_evidence=suggestion_evidence,
    )


def derive_from_connected_sources(conn, project_id: str) -> tuple[None, str]:
    """Look for a currency a connected source actually declared.

    Returns ``(None, reason)`` today, always. No table under the `app` schema
    stores a connected account's declared currency, so there is nothing to read
    -- and answering with a constant here is exactly the defect this module was
    written to remove.

    The reason string is meant to be shown, not swallowed: a Project whose
    currency is a platform fallback should say why no source could suggest one.
    """
    del conn, project_id  # nothing to read yet; the parameters pin the contract
    return (
        None,
        "no connected source declares an account currency yet: the platform has "
        "no store for it, so no currency can be suggested from evidence",
    )


PREFERENCES_INSERT_SQL = """
    INSERT INTO app.project_preferences
        (project_id, canonical_currency, reporting_timezone,
         canonical_currency_origin, canonical_currency_confirmation_status,
         reporting_timezone_origin, reporting_timezone_confirmation_status)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (project_id) DO NOTHING
"""


def preferences_insert_params(
    project_id: str,
    currency: PreferenceDecision,
    timezone: PreferenceDecision,
) -> tuple:
    """Build the parameter tuple for :data:`PREFERENCES_INSERT_SQL`."""
    for decision in (currency, timezone):
        if decision.origin not in VALID_ORIGINS:
            raise ValueError(f"unknown preference origin: {decision.origin!r}")
    return (
        project_id,
        currency.value,
        timezone.value,
        currency.origin,
        currency.confirmation_status,
        timezone.origin,
        timezone.confirmation_status,
    )


def insert_project_preferences(
    cur,
    project_id: str,
    currency: PreferenceDecision,
    timezone: PreferenceDecision,
) -> None:
    """Write the two Project defaults with the provenance they earned.

    Every Project-creation path goes through here, so there is one place where
    an origin is decided and one place to change if the rules move.
    """
    cur.execute(
        PREFERENCES_INSERT_SQL,
        preferences_insert_params(project_id, currency, timezone),
    )


__all__ = [
    "CONFIRMATION_UNCONFIRMED",
    "ORIGIN_DEFAULT",
    "ORIGIN_OPERATOR",
    "ORIGIN_SUGGESTION",
    "PLATFORM_FALLBACK_CURRENCY",
    "PLATFORM_FALLBACK_TIMEZONE",
    "PREFERENCES_INSERT_SQL",
    "VALID_ORIGINS",
    "PreferenceDecision",
    "decide",
    "decide_currency",
    "decide_timezone",
    "derive_from_connected_sources",
    "insert_project_preferences",
    "preferences_insert_params",
]
