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

Windows/CI note: all message strings use ASCII-safe characters only (except the French user
message body, which is UTF-8 like the CURRENCY_GAP message it mirrors).
"""

from __future__ import annotations

import logging
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
    renders it without a new currency-resolution branch: ``code`` / ``message`` (French, like
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
        "message": (
            "Aucun fuseau de reporting resolvable pour ce flux a grain journalier : "
            "aucun flux ne declare le fuseau utilise par la source pour tracer ses "
            "frontieres de journee. Fail-closed : un jour qu'on ne sait pas situer sur "
            "un fuseau n'est ni aligne ni compare avec un autre flux tant que ce fuseau "
            "n'est pas resolu (declarez le fuseau de reporting de la source par flux)."
        ),
        "affected_streams": affected,
        # --- shape-aligned with CURRENCY_GAP (datamodel) ---
        "severity": "refusal",
        "monetary": bool(field.get("monetary")) if isinstance(field, dict) else False,
        "conflicting_currencies": None,  # not a currency conflict
        "metric": field.get("name") if isinstance(field, dict) else None,
        "resolvable_via": "source_timezone_declaration",
    }
