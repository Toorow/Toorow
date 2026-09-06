"""toorow -- the per-source time-context CAPTURE adapter (Story 39.7).

This is the CAPTURE half of the timezone module. It generalizes the reference per-row
report-timezone capture (a connector reads, from its own provider metadata at pull, the
exact zone the source drew its reporting-day boundaries on, and stores it per raw row) into
a source-agnostic CONTRACT every connector follows -- exactly parallel to how Story 39.2
promoted an ad-hoc micros handling into the per-source money ADAPTER
(``server/core/money.py``).

Where 39.2 declares the money adapter's INPUT (``native_unit`` on a per-FIELD ``money``
sub-object), 39.7 declares the time-context adapter's INPUT: a per-DESCRIPTOR
``time_context`` sub-object on ``source_capabilities`` saying WHERE the connector reads
the exact report timezone the source used to draw its reporting-day boundaries, and WHAT
its fallback posture is when the zone cannot be determined. The report timezone is a
property of the datastream as a whole (every figure in one pull is drawn on the same day
boundary), so the declaration lives once at the DESCRIPTOR level -- not repeated per field
(contrast 39.2's per-field money).

This module is the Python home of the contract's BRAIN (three faces):

  * ``is_valid_iana`` -- true iff a candidate zone resolves via the stdlib ``zoneinfo``
    (py3.9+; NO third-party tz dependency). Null/empty/unknown => False.
  * ``resolve_capture`` -- the CONTRACT BRAIN. INPUT = the connector's ``time_context``
    declaration + the zone actually captured at pull (may be None). OUTPUT = a normalized
    provenance dict with EXACTLY three legal outcomes (E39-NFR02, fail-CLOSED):
      (1) a valid IANA zone   -> ``{report_timezone: zone, assumed: False, gap: False}``
      (2) an EXPLICITLY-declared assumption, TAGGED ``assumed=True`` in provenance
          (never silent) -> ``{report_timezone: assumed_zone, assumed: True, gap: False}``
      (3) a typed TIMEZONE_GAP -> ``{report_timezone: None, assumed: False, gap: True}``
    There is NO fourth branch that silently writes ``'UTC'`` or the project default. A day
    you cannot place on a timezone is a day you cannot honestly align later (Story 39.8),
    so an undetermined capture under the ``gap`` posture refuses -- it never invents a zone.
  * ``timezone_gap`` -- build the typed ``TIMEZONE_GAP`` conflict dict, shape-aligned with
    ``CURRENCY_GAP`` (``server/core/datamodel.py``): ``code`` / ``severity`` / ``metric`` /
    ``affected_streams`` / ``resolvable_via``. Returns None when every used-by stream
    already carries a resolvable report timezone.

STATELESS & SOURCE-AGNOSTIC (AD-2, E39-NFR05): this module contains **ZERO**
provider/connector names and names no provider field. The ``locus`` vocabulary
(``network`` / ``property`` / ``account`` / ``fixed`` / ``none``) is an ABSTRACT source of
the zone, never a provider's field name -- the connector reads its OWN provider field and
hands core only the captured zone string. Core owns "is it valid IANA?", "what on null?",
"is this a gap?".

SCOPE (Story 39.7): CAPTURE only. This module records the immutable source report timezone
as provenance (E39-AD2 -- the source zone is never rewritten). It does NOT re-slice days,
does NOT ``convert_timezone()`` at day grain (dishonest at DATE grain -- no sub-day data
exists; HG-4), and does NOT signal cross-source day-offsets. Signalling the offset +
exposing the source adjustment lever is Story 39.8, which DEPENDS on this capture.

Design mirrors ``server/core/money.py`` (stateless, zero-provider-name core helper, pure
functions kept separate from I/O so the fail-closed invariant is testable offline without a
warehouse or a network call).

Windows/CI note: all message strings are ASCII-safe. The user-facing body used to be
French, mirroring CURRENCY_GAP; Story 48.3 AC11 made English the rule for every string
that reaches a screen, and this one does.
"""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The abstract locus vocabulary + fallback postures (the contract's INPUT enum).
#
# AD-2: these are ABSTRACT sources of the report timezone, never provider field names.
#   - network/property/account = read from provider metadata at pull (the connector reads
#     its OWN provider field and hands core the captured zone);
#   - fixed  = the provider always reports in one declared zone (fixed_zone);
#   - none   = the provider exposes no report timezone (=> a TIMEZONE_GAP).
# ---------------------------------------------------------------------------

LOCUS_NETWORK = "network"
LOCUS_PROPERTY = "property"
LOCUS_ACCOUNT = "account"
LOCUS_FIXED = "fixed"
LOCUS_NONE = "none"

# Loci that read a live zone from provider metadata at pull (captured_zone may be None).
_METADATA_LOCI = frozenset({LOCUS_NETWORK, LOCUS_PROPERTY, LOCUS_ACCOUNT})

FALLBACK_GAP = "gap"  # refuse to assume -> surface TIMEZONE_GAP (fail-closed, the default)
FALLBACK_ASSUME = "assume"  # record an explicitly-declared assumed_zone as a TAGGED assumption

# The typed refusal code (shape-aligned with datamodel.CURRENCY_GAP).
TIMEZONE_GAP_CODE = "TIMEZONE_GAP"

# Keys a used-by row MAY carry a resolved report-timezone provenance signal under
# (source-agnostic; never a provider field name, AD-2). Mirrors datamodel's
# _CURRENCY_PROVENANCE_KEYS: today the used-by SELECT carries none of these (so a
# date-grain field trips the gap), and this tuple is the DOCUMENTED SEAM for the read layer
# to surface the per-stream report_timezone provenance captured here.
_TIMEZONE_PROVENANCE_KEYS = ("report_timezone", "reporting_timezone", "source_timezone")

#: THE key a ``pull()`` result returns its OBSERVED report timezone under (AI-161).
#:
#: 39.7 declared the capture contract and connectors honoured half of it: they resolve the
#: zone and bury it in the landed row. The pull result carried ``{pull_id, row_count,
#: date_from, date_to}`` and nothing else, so the worker -- the only place that knows the
#: datastream, the project and that the run SUCCEEDED -- could not record what the pull
#: observed. ``time_boundary.record_boundary_evidence`` therefore had zero callers while
#: ``capability_compilers`` read the table it fills, and told the operator "Run this
#: Datastream so its publication records the source day boundary". They could run it
#: forever.
#:
#: The zone is per-DATASTREAM, not per-row: every figure in one pull is drawn on the same
#: day boundary (that is why 39.7 put the declaration at descriptor level). So the pull
#: returns ONE zone, resolved through ``resolve_capture``, or None when it observed none --
#: and None is a RESULT, recorded as such, never a silence.
PULL_RESULT_TIMEZONE_KEY = "report_timezone"


def observed_zone_from_pull(result: Any) -> str | None:
    """Read the observed report timezone a ``pull()`` returned, or None (PURE, AI-161).

    Fail-closed like every other read here: a blank string, a non-string, a missing key or
    a non-dict result all mean "this pull observed no zone", never a fabricated default.
    Validation is deliberately NOT done here -- ``record_boundary_evidence`` canonicalises,
    and a zone that fails canonicalisation must be recorded as the gap it is rather than
    dropped on the floor by the reader.
    """
    if not isinstance(result, dict):
        return None
    value = result.get(PULL_RESULT_TIMEZONE_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------
# is_valid_iana -- the zone validator (stdlib zoneinfo, PURE, offline).
# ---------------------------------------------------------------------------


def is_valid_iana(zone: str | None) -> bool:
    """True iff *zone* resolves as an IANA time zone via stdlib ``zoneinfo`` (PURE).

    Null / empty / whitespace-only / unknown-name => False (fail-closed: an unresolvable
    zone is no zone). No third-party tz library -- ``zoneinfo`` ships in the stdlib (py3.9+)
    and reads the OS/tzdata database. Offset-only strings like ``'GMT+2'`` and made-up names
    like ``'Not/AZone'`` are NOT valid IANA zones and return False.
    """
    if not isinstance(zone, str):
        return False
    candidate = zone.strip()
    if not candidate:
        return False
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # ValueError: a malformed key (e.g. contains a null byte / bad path component).
        # OSError: the tzdata backing store could not be read for that key.
        return False
    return True


# ---------------------------------------------------------------------------
# resolve_capture -- the CONTRACT BRAIN. Total function, three legal outcomes.
#
# The fail-closed invariant (E39-NFR02, AC3) is STRUCTURAL, not merely tested: the ONLY
# code paths that yield a non-null report_timezone with assumed=False are (a) the capture
# was a valid IANA zone, or (b) locus='fixed' with a valid declared fixed_zone. Every other
# path yields either a TAGGED assumption (assumed=True) or gap=True with report_timezone
# None. No branch writes 'UTC' / the project default.
# ---------------------------------------------------------------------------


def _gap_result() -> dict:
    """The fail-closed outcome: no zone, not an assumption, a gap. NEVER a silent default."""
    return {"report_timezone": None, "assumed": False, "gap": True}


def _zone_result(zone: str, *, assumed: bool) -> dict:
    """A resolved outcome: a valid IANA zone, tagged assumed or not."""
    return {"report_timezone": zone, "assumed": assumed, "gap": False}


def resolve_capture(declared: dict | None, captured_zone: str | None) -> dict:
    """Resolve a pull's report-timezone provenance from the declaration + the captured zone.

    The contract brain (PURE, offline). Returns a normalized provenance dict with EXACTLY
    three legal shapes (fail-CLOSED, E39-NFR02):

      * a valid captured IANA zone
            => ``{"report_timezone": <zone>, "assumed": False, "gap": False}``
      * ``locus='fixed'`` with a valid ``fixed_zone`` (declared, deterministic -- no live read)
            => ``{"report_timezone": <fixed_zone>, "assumed": False, "gap": False}``
      * captured None/invalid AND ``fallback='assume'`` with a valid ``assumed_zone``
            => ``{"report_timezone": <assumed_zone>, "assumed": True, "gap": False}``
              (the assumption is RECORDED, TAGGED -- never silent)
      * captured None/invalid AND (``fallback='gap'`` OR ``locus='none'`` OR no declaration
        OR an invalid declared fixed/assumed zone)
            => ``{"report_timezone": None, "assumed": False, "gap": True}``
              (fail-closed -- NEVER returns ``'UTC'`` / the project default)

    Args:
        declared:      the connector's ``time_context`` declaration
                       (``{locus, fallback, fixed_zone?, assumed_zone?}``) or None (absent =>
                       treated as ``locus='none'``: a gap for its date-grain figures).
        captured_zone: the zone actually read from provider metadata at pull, or None when
                       the metadata call was unavailable / the field was absent.

    Returns:
        dict: ``{"report_timezone": str | None, "assumed": bool, "gap": bool}``.
    """
    # No declaration => the connector captures no report timezone yet (locus='none' honest gap).
    if not isinstance(declared, dict):
        return _gap_result()

    locus = declared.get("locus")
    fallback = declared.get("fallback")

    # locus='fixed': the provider always reports in one DECLARED zone -> deterministic, no
    # live capture is consulted. A malformed/invalid fixed_zone fails closed (never persisted).
    if locus == LOCUS_FIXED:
        fixed_zone = declared.get("fixed_zone")
        if is_valid_iana(fixed_zone):
            return _zone_result(fixed_zone.strip(), assumed=False)
        return _gap_result()

    # locus='none': the provider exposes no report timezone -> an honest gap (no live read).
    if locus == LOCUS_NONE:
        return _gap_result()

    # locus in {network, property, account}: a live metadata read was attempted at pull.
    # A valid captured zone is source truth -> passthrough (immutable provenance, E39-AD2).
    if locus in _METADATA_LOCI and is_valid_iana(captured_zone):
        return _zone_result(captured_zone.strip(), assumed=False)

    # Undetermined (captured None/invalid, or an unrecognized locus): resolve the posture.
    if fallback == FALLBACK_ASSUME:
        assumed_zone = declared.get("assumed_zone")
        if is_valid_iana(assumed_zone):
            # The assumption is RECORDED and TAGGED -- this is the ONLY non-null zone with a
            # capture that failed, and it is explicit, never silent (E39-NFR02).
            return _zone_result(assumed_zone.strip(), assumed=True)
        # 'assume' posture but no valid assumed_zone declared -> still fail closed.
        return _gap_result()

    # fallback='gap' (the default posture) or anything unrecognized -> the typed gap.
    return _gap_result()


# ---------------------------------------------------------------------------
# timezone_gap -- the typed read-time refusal (shape-aligned with CURRENCY_GAP).
# ---------------------------------------------------------------------------


def _used_by_timezone(ub: dict) -> str | None:
    """Return the resolved report timezone a used-by row carries (source-agnostic), or None.

    Reads only generic provenance keys (never a provider's field name, AD-2). A blank/None
    value counts as ABSENT (fail-closed: an empty zone is no zone, E39-NFR02).
    """
    if not isinstance(ub, dict):
        return None
    for key in _TIMEZONE_PROVENANCE_KEYS:
        val = ub.get(key)
        if isinstance(val, str):
            stripped = val.strip()
            if stripped:
                return stripped
    return None


def used_by_has_timezone(ub: dict) -> bool:
    """True iff a used-by row carries ANY resolvable report-timezone signal (source-agnostic).

    Thin predicate over ``_used_by_timezone`` (fail-closed on blank/None, AD-2)."""
    return _used_by_timezone(ub) is not None


def declared_zone(declared: dict | None) -> str | None:
    """Return the DETERMINISTIC report timezone a declaration alone fixes, or None (PURE).

    A pure read over the ``time_context`` declaration -- NO warehouse, NO live capture:
      * ``locus='fixed'`` with a valid ``fixed_zone``  -> that zone (the provider always
        reports in one declared zone);
      * ``fallback='assume'`` with a valid ``assumed_zone`` -> that zone (the explicitly
        declared assumption -- the datastream's default report timezone when the live read
        is undetermined);
      * everything else (``network``/``property``/``account`` = a live per-pull read,
        ``none``, no declaration, invalid declared zone) -> None (the zone is not fixed by
        the declaration alone; the live captured per-row provenance -- Task 3's warehouse
        column -- is the source of truth, resolved via ``resolve_capture`` at pull).

    This never invents ``'UTC'``/a default: an undeclared/live-only locus returns None.
    """
    if not isinstance(declared, dict):
        return None
    locus = declared.get("locus")
    if locus == LOCUS_FIXED:
        fixed_zone = declared.get("fixed_zone")
        return fixed_zone.strip() if is_valid_iana(fixed_zone) else None
    if declared.get("fallback") == FALLBACK_ASSUME:
        assumed_zone = declared.get("assumed_zone")
        return assumed_zone.strip() if is_valid_iana(assumed_zone) else None
    return None


#: Loci whose zone is a SETTING at the source, hence changeable there. Heuristic shipped
#: by story 48.3 (``capability_compilers._lever``), kept here as the fallback. It is wider
#: than epic 39's wording ("dimension filters (rare)") and that is deliberate: the question
#: put to the user is "can I act at the source?", and a network's or a property's zone is
#: indeed changed at the source.
_LEVER_LOCI = frozenset({LOCUS_NETWORK, LOCUS_PROPERTY, LOCUS_ACCOUNT})

_LEVER_LOCUS_HINT = "The source exposes a reporting-timezone setting; change it at the source."
_LEVER_NONE_HINT = "This source is fixed on its detected boundary; no adjustment lever exists."


def resolve_lever(declaration: dict | None, observed: Any = None) -> dict:
    """THE adjustment-lever resolution -- one, for the whole product (AI-132).

    THERE WERE TWO, AND THEY CONTRADICTED EACH OTHER. ``capability_compilers._lever``
    (shipped under 48.3) derived the lever from the locus; a second resolution added later
    for the day-offset signal required an explicit declaration. On a declaration carrying
    ``locus='network'`` the capability screen said "a lever exists" while the mapping signal
    said "no lever available" -- the same product, two opposite answers about the same
    source. Two producers, one of them wrong, is worse than none: nobody knows which to
    believe.

    Precedence, strongest first:

    1. **what a run OBSERVED** (``observed.adjustment_lever``) -- a declared lever no run
       ever exercised is a promise, not a lever;
    2. **an EXPLICIT declaration** (``time_context.adjustment_lever``) -- strictly more
       specific than the heuristic, so it wins over it. It is the only way to state "this
       source exposes a setting in its dimension filters", the rare case epic 39 describes,
       and equally the only way to say NO where the locus would wrongly say yes;
    3. **the locus** (``network``/``property``/``account``) -- 48.3's fallback;
    4. otherwise: no lever, which is a SENTENCE and not a silence.

    Returns 48.3's shape (``{available, locus, origin, hint}``): that is what the capability
    screen already renders, and changing a shipped shape to unify two paths would make the
    screen -- which was correct -- pay for the merge.
    """
    locus = (declaration or {}).get("locus") if isinstance(declaration, dict) else None

    observed_lever = getattr(observed, "adjustment_lever", None) if observed is not None else None
    if observed_lever:
        lever = dict(observed_lever)
        if lever.get("available") is not None:
            lever.setdefault("origin", "observed")
            lever.setdefault("locus", locus)
            return lever

    declared = lever_from_declaration(declaration)
    if declared["has_lever"]:
        return {
            "available": True,
            "locus": locus,
            "origin": "declaration",
            "hint": declared["lever_hint"],
        }
    # An explicit declaration that says NO also decides: it is more specific than the locus,
    # and ignoring it would make it impossible to contradict the heuristic.
    if _declares_lever_explicitly(declaration):
        return {"available": False, "locus": locus, "origin": "declaration",
                "hint": _LEVER_NONE_HINT}

    if locus in _LEVER_LOCI:
        return {"available": True, "locus": locus, "origin": "declaration",
                "hint": _LEVER_LOCUS_HINT}
    return {
        "available": False,
        "locus": locus,
        "origin": "declaration" if declaration else "none",
        "hint": _LEVER_NONE_HINT,
    }


def _declares_lever_explicitly(declared: dict | None) -> bool:
    """True when the connector took a position on the lever, whichever way it answered."""
    if not isinstance(declared, dict):
        return False
    lever = declared.get("adjustment_lever")
    return isinstance(lever, dict) and lever.get("available") is not None


def signal_lever(declaration: dict | None, observed: Any = None) -> dict:
    """The same resolution, in the shape the signal engine expects.

    ``{has_lever, lever_hint}``. An available lever with NO hint is reported as absent:
    pointing someone at "somewhere in the source" is worse than saying nothing, and the pure
    engine already takes that posture.
    """
    lever = resolve_lever(declaration, observed)
    hint = lever.get("hint")
    if lever.get("available") is not True or not isinstance(hint, str) or not hint.strip():
        return {"has_lever": False, "lever_hint": None}
    return {"has_lever": True, "lever_hint": hint.strip()}


def lever_from_declaration(declared: dict | None) -> dict:
    """Return the source's ADJUSTMENT-LEVER posture from its declaration (PURE) -- AI-132.

    Answers the ratified question *"can the user act at the source?"*
    (`capabilities/reporting-timezone.md`: « the user cannot tell whether the source offers an
    adjustment lever »). The epic is precise about what a lever IS, and it is narrow: *"a few
    platforms expose a report-timezone setting in their dimension filters (rare); when present,
    the user is pointed to it to act at the source; when absent, the datastream is reported as
    'fixed on the detected timezone, no lever available'"*
    (`epic-39-shared-money-and-timezone-modules.md:191-193`).

    So a lever is NOT derivable from the locus. `locus='property'` means the zone is read from
    property metadata -- it says nothing about whether a report query can override it. Deriving
    one from the other would have been inventing a product fact, which is why it is DECLARED::

        "time_context": {
          "locus": "network",
          "fallback": "gap",
          "adjustment_lever": {"available": true, "hint": "..."}
        }

    A lever declared WITHOUT a hint is reported as absent: pointing a user at "somewhere in the
    source" is worse than saying there is nothing to point at. Same posture the pure engine
    already takes (`timezone_signal.py`), applied one step earlier so both agree.

    AD-2 holds for CORE, not for a connector describing its OWN provider: this function never
    names a provider; the hint is authored by the connector that owns that provider.

    Returns ``{"has_lever": bool, "lever_hint": str | None}`` -- never None, so a caller can
    always state a posture. No declaration => no lever, which is a STATEMENT ("fixed on the
    detected timezone, no lever available"), not a silence.
    """
    absent = {"has_lever": False, "lever_hint": None}
    if not isinstance(declared, dict):
        return absent
    lever = declared.get("adjustment_lever")
    if not isinstance(lever, dict) or lever.get("available") is not True:
        return absent
    hint = lever.get("hint")
    if not isinstance(hint, str) or not hint.strip():
        # Declared but unusable: an "available" lever nobody can find is not a lever.
        return absent
    return {"has_lever": True, "lever_hint": hint.strip()}


def lever_for_module(module_name: str | None) -> dict:
    """Resolve a module's adjustment-lever posture from its manifest (fail-soft) -- AI-132.

    Keyed on ``module_name`` and NOT on ``datastream_id`` on purpose: a used-by row already
    carries the module (``datamodel``'s SELECT), and the day-offset signal resolves a posture
    for EVERY stream it reports. Going through ``datastream_id`` would mean one DB round-trip
    per stream to learn something the manifest already knows.

    FAIL-SOFT, like ``report_timezone_for_datastream``: any registry/loader problem yields the
    absent posture rather than a crash -- an unknown lever reads as no lever, never as one.
    """
    if not isinstance(module_name, str) or not module_name.strip():
        return {"has_lever": False, "lever_hint": None}
    try:
        from core.main import _loaded_modules  # noqa: PLC0415

        manifest = next(
            (m.manifest for m in _loaded_modules
             if getattr(m, "name", None) == module_name.strip()),
            None,
        )
        if not isinstance(manifest, dict):
            return {"has_lever": False, "lever_hint": None}
        caps = manifest.get("source_capabilities")
        tc = caps.get("time_context") if isinstance(caps, dict) else None
        # `signal_lever` et non `lever_from_declaration` : LA resolution unique, celle
        # que l'ecran de capacite utilise aussi. Court-circuiter ici recreerait la
        # contradiction que cette fusion supprime.
        return signal_lever(tc)
    except Exception as exc:  # noqa: BLE001 -- fail-soft, never crash the read path.
        logger.warning("report_timezone: lever_for_module fell back to absent for %s: %s",
                       module_name, exc)
        return {"has_lever": False, "lever_hint": None}


def report_timezone_for_datastream(datastream_id: str) -> str | None:
    """Return the resolvable report timezone for a datastream, or None (fail-soft accessor).

    ORCHESTRATOR-DECISION accessor (Story 39.7), mirroring
    ``metric_semantics.is_metric_monetary``: a thin, callable seam the read layer / Story 39.8
    (the SIGNAL half) keys on -- 39.8 swaps its own resolution to this accessor.

    Resolution (manifest/declaration read -- a warehouse projection seed is DEFERRED per the
    orchestrator decision; add one only if 39.8 proves it necessary):
      datastream_id -> module_name (DB) -> the module manifest's
      ``source_capabilities.time_context`` declaration -> ``declared_zone(...)``.

    Returns the zone a declaration DETERMINISTICALLY fixes (``locus='fixed'`` fixed_zone, or a
    declared ``assume`` assumed_zone); None for a live-read locus (network/property/account --
    the zone is per-pull warehouse provenance, not a manifest fact), for ``locus='none'``, for
    no declaration, or on any lookup failure. NEVER returns ``'UTC'``/a silent default
    (E39-NFR02): an unresolved datastream returns None, and the caller fails closed on None.

    FAIL-SOFT (mirrors ``is_metric_monetary``): any DB/loader error -> None, never a crash.
    Source-agnostic (AD-2): reads only the ABSTRACT ``time_context`` declaration; it never
    names a provider or reads a provider field here.
    """
    try:
        declared = _time_context_for_datastream(datastream_id)
    except Exception as exc:  # noqa: BLE001 -- fail-soft, never crash the read path.
        logger.warning(
            "report_timezone: report_timezone_for_datastream fell back to None for %s: %s",
            datastream_id, exc,
        )
        return None
    return declared_zone(declared)


def _time_context_for_datastream(datastream_id: str) -> dict | None:
    """Resolve a datastream_id to its module's ``time_context`` declaration (DB + manifest).

    Isolated so ``report_timezone_for_datastream`` stays a thin fail-soft wrapper and tests can
    exercise ``declared_zone`` / ``resolve_capture`` purely (no DB). Uses the SAME seams as
    ``datastreams.backfill_datastreams``: the loaded-modules registry
    (``core.main._loaded_modules``) for manifests and ``core.db.get_connection`` for the
    datastream -> module_name lookup. Lazy
    imports keep this module import-light and free of a hard DB/loader dependency at load time.
    """
    module_name = _module_name_for_datastream(datastream_id)
    if not module_name:
        return None
    from core.main import _loaded_modules  # noqa: PLC0415

    manifest = next(
        (m.manifest for m in _loaded_modules if getattr(m, "name", None) == module_name),
        None,
    )
    if not isinstance(manifest, dict):
        return None
    caps = manifest.get("source_capabilities")
    if not isinstance(caps, dict):
        return None
    tc = caps.get("time_context")
    return tc if isinstance(tc, dict) else None


def _module_name_for_datastream(datastream_id: str) -> str | None:
    """Return the module_name feeding a datastream (DB read by id), or None. Fail-soft."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT module_name FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0].strip()
    return None


def timezone_gap(field: dict, used_by: list[dict]) -> dict | None:
    """Build the typed ``TIMEZONE_GAP`` conflict dict, or None when every stream carries a zone.

    Shape-aligned with ``datamodel.CURRENCY_GAP`` so ``conflict_resolutions.list_conflicts``
    renders it without a new currency-resolution branch: ``code`` / ``message`` (English, like
    the CURRENCY_GAP message) / ``affected_streams`` / ``severity='refusal'`` / ``metric`` /
    ``resolvable_via='source_timezone_declaration'``.

    Returns None (no conflict) when at least one feeding stream already carries a resolvable
    report timezone -- the gap fires ONLY when NO stream can place the day on a zone.
    """
    if any(used_by_has_timezone(ub) for ub in used_by):
        return None
    affected = [ub.get("datastream_name") or ub.get("module_name", "") for ub in used_by]
    return {
        "code": TIMEZONE_GAP_CODE,
        # English: this string reaches a screen (Story 48.3 AC11).
        "message": (
            "No reporting timezone resolves for this day-grain field: no feeding stream "
            "states the clock its source used to draw day boundaries. Fail closed -- a day "
            "that cannot be placed on a timezone is neither aligned nor compared with "
            "another stream until it is. Record the source reporting timezone per stream."
        ),
        "affected_streams": affected,
        # --- shape-aligned with CURRENCY_GAP (datamodel) ---
        "severity": "refusal",
        "monetary": bool(field.get("monetary")) if isinstance(field, dict) else False,
        "conflicting_currencies": None,  # not a currency conflict
        "metric": field.get("name") if isinstance(field, dict) else None,
        "resolvable_via": "source_timezone_declaration",
    }
