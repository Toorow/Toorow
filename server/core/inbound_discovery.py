"""The first delivery reveals the shape -- the link that inverted the journey.

THE ORDER THE PRODUCT PROMISES, and the order the code enforced.

The journey is: create an inbound Datastream, get its address, sit in draft, send
the first file, SEE ITS SHAPE, validate, and only then start processing. The code
enforced the reverse. ``inbound_ingest.ingest_inbound_file`` refuses any delivery
whose Datastream does not already carry a locked plan, a locked mapping and a
recorded executable projection -- its own refusal says "run at least one attended
import before enabling inbound ingest". So a person had to upload a file by hand
first, and email only worked afterwards. The emailed file could never be the
first one.

WHAT WAS ALREADY TRUE, measured before writing any of this, because three of the
four links turned out to work already:

  * an address IS issuable against a draft Datastream -- ``_get_datastream_status``
    selects only ``source_kind`` and ``org_id``, and checks no ``enabled`` flag
    despite a docstring claiming it does;
  * a delivery to a draft IS resolved -- ``resolve_by_token_hash`` checks the
    credential's own state and the Datastream's EXISTENCE, nothing more;
  * the bytes ARE durably kept -- receipt, per-attachment raw import, scan
    verdict, all of it (Stories 38.8 to 38.10).

Only the fourth link was missing: what happens next. The delivery reached
``ingest_inbound_file``, was refused, and the attachment landed FAILED. The file
was safe, hashed, scanned and useless.

WHAT THIS MODULE DOES. It turns that refusal into an OBSERVATION. Given a draft
Datastream, it reads the most recent accepted delivery and returns the shape --
field names, types, nullability, a schema fingerprint, a bounded row-count
bucket -- in EXACTLY the payload the staged-upload observer returns for an
uploaded file. Same adapter contract, same normalisation, same review screen.
(The observer itself lives behind the setup-adapter seam and is deliberately
not named here: AD-2 keeps that vocabulary out of core, and a docstring counts
-- naming it once is how this module first broke the boundary guard.)

The wizard cannot tell whether the shape came from an upload or from an email,
and that is the point: the delivery channel is transport, and transport must not
change what governance sees.

WHAT IT DOES NOT DO, and must not:

  * It does NOT publish. No plan, no mapping, no pointer, no row in a mart. It
    describes; a human still confirms.
  * It does NOT relax the ingest guard. ``ingest_inbound_file`` still refuses an
    unconfigured Datastream, and it should: that guard protects a publication
    nobody reviewed. What changes is that a DRAFT no longer reaches the guard --
    it takes this path instead, because a draft has by definition nothing to
    publish against yet.
  * It does NOT return a row. Field names and types are schema; the values
    behind them are the tenant's data, and a discovery payload travelling into a
    setup review is the last place they belong (FORBIDDEN_KEYS in
    ``datastream_setup_observations`` names ``sample_rows`` and ``raw_sample``
    explicitly).

ASCII-only source (AI-03). Pure reads; the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The adapter identity carried into the observation. Distinct from the upload
#: adapter (`managed_feed.file.readonly.v1`) so an auditor can tell WHERE a
#: shape came from -- the payload is identical, the provenance is not.
ADAPTER_REF = "managed_feed.received_file.readonly.v1"

#: Stable reasons a first delivery cannot be observed.
NO_DELIVERY_YET = "no_delivery_received_yet"
NO_READABLE_DELIVERY = "delivery_not_parseable"
NO_BYTES = "retained_bytes_unavailable"

#: Stable reasons a CHANNEL CONTRACT clause is not settled yet (story 57.3).
#: Beside the three above rather than mixed into them: those say why a delivery
#: could not be described, these say why a clause of the promise is still blank.
#: Neither is a failure, and each names a different authority to repair it.
NO_INBOUND_DOMAIN = "inbound_domain_not_configured"
NOT_ADDRESSABLE_YET = "channel_not_addressable_yet"
NO_ARRIVAL_EXPECTATION = "arrival_expectation_not_declared"

#: Raw-import states whose bytes are worth describing. A REJECTED attachment
#: failed the scan gate, and describing a file the gate refused would put its
#: shape on a review screen as if it were a candidate.
_OBSERVABLE_STATES = ("ACCEPTED", "LANDED", "FAILED")


def _fingerprint(value: Any) -> str:
    """Stable hash of a JSON-canonical value. Mirrors the upload adapter."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _row_bucket(count: int) -> str:
    """A BUCKET, never an exact count.

    An exact row count on a setup screen is a fact about the tenant's data that
    the review does not need in order to decide whether the shape is right.

    THE VOCABULARY IS COPIED, not chosen. The staged-upload observer and the
    activation driver both answer "0" / "1-99" / "100-999" / "1000-9999" /
    "10000+", and this module claims a wizard cannot tell which channel produced
    a shape. A different set of strings here would have made that claim false on
    its most visible field -- which is what the first version of this function
    did, while its own docstring said it mirrored them.
    """
    if count <= 0:
        return "0"
    if count < 100:
        return "1-99"
    if count < 1_000:
        return "100-999"
    if count < 10_000:
        return "1000-9999"
    return "10000+"


def _latest_observable_raw_import(conn, *, datastream_id: str) -> dict[str, Any] | None:
    """The most recent delivery worth describing, newest first.

    Deliberately NOT the first-ever delivery. A sender who gets it wrong twice
    should see their second file, not be told forever about their first.
    """
    from core.inbound_raw_imports import list_raw_imports  # noqa: PLC0415

    for row in list_raw_imports(conn, datastream_id=datastream_id, limit=25):
        if row.get("state") in _OBSERVABLE_STATES and row.get("quarantine_uri"):
            return row
    return None


def observe_first_delivery(
    conn,
    *,
    datastream_id: str,
    store=None,  # noqa: ANN001
) -> dict[str, Any]:
    """Describe the shape of the latest received file, as a setup observation.

    Returns the adapter payload the setup-observation seam already understands::

        {"adapter_ref": ..., "safe_metadata": {...}, "coverage": {...},
         "exceptions": [{"code": ...}]}

    Never raises for an absent or unreadable delivery: an unavailability is
    reported through ``exceptions`` with a stable code and an empty field list,
    because the wizard screen that calls this is the screen a person opens to
    find out WHY nothing is happening.
    """

    def _unavailable(code: str) -> dict[str, Any]:
        return {
            "adapter_ref": ADAPTER_REF,
            "safe_metadata": {"fields": []},
            "coverage": {"schema": "unavailable", "rows": "unavailable"},
            "exceptions": [{"code": code}],
        }

    raw = _latest_observable_raw_import(conn, datastream_id=datastream_id)
    if raw is None:
        return _unavailable(NO_DELIVERY_YET)

    try:
        if store is None:
            from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

            store = open_quarantine_store()
        data = store.get(raw["quarantine_uri"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "inbound_discovery: retained bytes unreadable ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return _unavailable(NO_BYTES)

    parsed, parse_error = _parse_for_shape(data, raw)
    if parsed is None:
        return _unavailable(parse_error or NO_READABLE_DELIVERY)

    fields = [
        {
            "name": column.name,
            "field_id": column.name,
            "type": column.detected_type,
            "nullable": column.null_count > 0,
        }
        for column in (parsed.columns or [])[:200]
    ]

    return {
        "adapter_ref": ADAPTER_REF,
        "safe_metadata": {
            # The content hash IS the reference here, where an upload would
            # carry a staged_asset_ref: the file was not staged by anyone, it
            # arrived, and its content address is what identifies it.
            "content_hash": parsed.content_hash,
            "schema_hash": _fingerprint(fields),
            "detected_format": parsed.encoding,
            "fields": fields,
            "row_count_bucket": _row_bucket(parsed.detected_row_count),
            "append_supported": False,
        },
        "coverage": {"schema": "available", "rows": "bounded_count_only"},
        "exceptions": [],
    }


def _parse_for_shape(data: bytes, raw: dict[str, Any]):
    """Parse only far enough to name columns and retain a typed failure code.

    Bounded on purpose: a setup screen needs the shape, not the dataset, and the
    file has already been through the 38.10 gate -- this must not be the place
    that spends the budget the gate exists to protect.
    """
    from core.csv_excel_import import (  # noqa: PLC0415
        FORMAT_CSV,
        FORMAT_SAV,
        CsvExcelImportError,
        detect_format,
        parse_csv,
        parse_excel,
    )

    try:
        from core.inbound_sav import parse_sav

        fmt = detect_format(raw.get("filename"), data)
        if fmt == FORMAT_CSV:
            return parse_csv(data, max_rows=10_000), None
        if fmt == FORMAT_SAV:
            return parse_sav(data, max_rows=10_000), None
        return parse_excel(data, max_rows=10_000), None
    except CsvExcelImportError as exc:
        logger.debug("inbound_discovery: tabular parse refused: %s", exc.code)
        return None, exc.code
    except Exception as exc:  # noqa: BLE001 -- an unparseable delivery IS the answer
        logger.debug("inbound_discovery: tabular parse failed: %s", type(exc).__name__)
        return None, NO_READABLE_DELIVERY


# ---------------------------------------------------------------------------
# The delivery posture: which of the two paths a delivery takes.
# ---------------------------------------------------------------------------

#: A Datastream that has never been activated. Its delivery is DESCRIBED.
POSTURE_DISCOVERY = "discovery"
#: A configured Datastream. Its delivery is INGESTED, as it always was.
POSTURE_INGEST = "ingest"


def delivery_posture(conn, *, datastream_id: str) -> str:
    """Should this Datastream's delivery be ingested, or described?

    The rule is one fact, not a heuristic: a Datastream in ``draft`` lifecycle
    has never published anything, so there is nothing for a delivery to be
    ingested INTO. Its first file can only be evidence for a human decision.

    Everything else keeps the old behaviour, including the refusals. An ACTIVE
    Datastream missing a plan or a mapping is a real misconfiguration and must
    still fail loudly -- routing it here would replace an actionable failure
    with a silent description, which is the trade this repository keeps having
    to unwind.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lifecycle_state FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- unreadable state is not a draft
        logger.warning(
            "inbound_discovery: lifecycle unreadable ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return POSTURE_INGEST

    if row is None:
        return POSTURE_INGEST
    return POSTURE_DISCOVERY if row[0] == "draft" else POSTURE_INGEST


# ---------------------------------------------------------------------------
# The channel contract: what an inbound channel PROMISES, story 57.3.
# ---------------------------------------------------------------------------
#
# A CONTRACT IS NOT AN OBSERVATION, and that distinction is the whole story. An
# email address and a webhook token cannot be listed: there is no provider to
# interrogate and nothing to enumerate until something arrives. So what step 1
# makes readable is a DECLARATION -- what the operator promises to send, how
# often, from where -- read back beside the state of the deployment that has to
# receive it.
#
# Four clauses, and each one says WHO settles it and WHEN rather than showing a
# blank. None of them is invented: an undeclared clause reads `not_declared`,
# never a default, never a zero.

#: How the address of an email channel is SHAPED. The address itself is a secret
#: minted against a Datastream (`inbound_credentials.issue_credential` requires a
#: `datastream_id`), so a draft can only ever show its form.
EMAIL_ADDRESS_FORM = "ds_<token>@{domain}"

#: The one authorization an inbound delivery has ever had. `inbound/receipt.py`
#: (`_TOKEN_RE`, `_RECIPIENT_RE`, `_FORBIDDEN_BODY`) accepts a delivery on the
#: token carried by the recipient address and refuses everything else with a
#: CONSTANT 403 -- identical for unsigned, oversize and unknown recipient, so an
#: attacker cannot learn which addresses exist. A declared sender list therefore
#: never changes that answer; it is applied at PROCESSING
#: (`core.inbound_sender_policy`), where a refusal can be named to the owner.
SENDER_TOKEN_ONLY = "token_only"
SENDER_DECLARED_LIST = "declared_list"


def normalize_allowed_senders(values: Any) -> list[str]:
    """Bounded, lowercased, de-duplicated sender declarations.

    Accepts a full address (``person@team.tld``) or a bare domain
    (``team.tld``). Nothing is validated against a provider: this is what the
    operator promises, not what a directory confirms.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        entry = value.strip().lower().lstrip("@")
        if entry and entry not in seen and len(entry) <= 320:
            seen.append(entry)
        if len(seen) >= 50:
            break
    return seen


def _declared_interval(declared: dict[str, Any] | None) -> int | None:
    """The arrival interval the operator declared, or None. NEVER a default.

    A default here is exactly the defect story 57.3 removes from
    ``datastream_activation``: a monitor armed on a number nobody gave will
    alert, or stay silent, without anyone having chosen.
    """
    raw = (declared or {}).get("expected_interval_minutes")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return minutes if 1 <= minutes <= 525_600 else None


def _inbound_domain(conn) -> str | None:
    """The verified inbound domain of this deployment, or None.

    The SAME read `datastream_setup_observations.get_source_options` runs for the
    channel list, so the contract block and the channel option can never disagree
    about whether this deployment can receive anything.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.domain FROM app.connector_domain_configs c "
            "JOIN app.connector_installations i ON i.id=c.installation_id "
            "WHERE c.connector_name='managed_feed' AND c.superseded_by IS NULL "
            "AND i.state IN ('READY','DEGRADED') "
            "ORDER BY c.config_version DESC LIMIT 1"
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _bound_template(conn, *, project_id: str, template_ref: str) -> dict[str, Any] | None:
    """The governed Template this channel says it will satisfy, or None.

    A REFERENCE, never a parsing rule. What the file must contain belongs to the
    Template itself (epic 22); the channel only declares WHICH one it owes.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT template_code,version,grain FROM app.file_source_templates "
            "WHERE id=%s AND project_id=%s AND is_active=TRUE",
            (template_ref, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"template_code": str(row[0]), "version": int(row[1]), "grain": row[2]}


def _issued_capability(conn, *, datastream_id: str) -> bool:
    """Does a live receiving capability exist for this Datastream?

    Reads STATE ONLY. No token, no hash, no suffix: this answers "is there an
    address" and nothing that could be used as one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastream_inbound_credentials "
            "WHERE datastream_id=%s AND state IN ('ACTIVE','ROTATING') LIMIT 1",
            (datastream_id,),
        )
        return cur.fetchone() is not None


def read_channel_contract(
    conn,
    *,
    project_id: str,
    channel: str,
    declared: dict[str, Any] | None = None,
    template_ref: str | None = None,
    datastream_id: str | None = None,
) -> dict[str, Any]:
    """Read the state of the four clauses of an inbound channel contract.

    PURE READ. It writes nothing, issues nothing, and calls no provider -- there
    is no provider to call. Every clause is either what the deployment really
    carries or the named absence of it.

    Returns::

        {"channel": ..., "domain": "configured"|"not_configured",
         "inbound_domain": str|None, "address_form": str|None,
         "capability": "issued"|"not_issued"|"not_addressable_yet",
         "format": "template_bound"|"not_declared", "template": {...}|None,
         "sender": "token_only"|"declared_list", "allowed_senders": [...],
         "arrival": "declared"|"not_declared",
         "expected_interval_minutes": int|None}
    """
    domain = _inbound_domain(conn)
    template = (
        _bound_template(conn, project_id=project_id, template_ref=str(template_ref))
        if template_ref
        else None
    )
    if datastream_id:
        issued = _issued_capability(conn, datastream_id=datastream_id)
        capability = "issued" if issued else "not_issued"
    else:
        # THE ORDINARY STATE OF A DRAFT, and it is not a gap. An address is minted
        # against a Datastream row, and a wizard draft has none -- so the step
        # shows the FORM of the address and where it will be issued.
        capability = "not_addressable_yet"
    senders = normalize_allowed_senders((declared or {}).get("allowed_senders"))
    interval = _declared_interval(declared)
    return {
        "channel": channel,
        "domain": "configured" if domain else "not_configured",
        "inbound_domain": domain,
        # A webhook has no dedicated URL: its capability IS a token
        # (`inbound_credentials`, `full_secret = raw_token` when the channel is
        # not email), so there is no address form to show for it.
        "address_form": (
            EMAIL_ADDRESS_FORM.format(domain=domain)
            if channel == "inbound_email" and domain
            else None
        ),
        "capability": capability,
        "format": "template_bound" if template else "not_declared",
        "template": template,
        "sender": SENDER_DECLARED_LIST if senders else SENDER_TOKEN_ONLY,
        "allowed_senders": senders,
        "arrival": "declared" if interval is not None else "not_declared",
        "expected_interval_minutes": interval,
    }
