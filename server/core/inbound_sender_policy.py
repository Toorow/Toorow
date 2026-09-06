"""The declared sender allowlist, and the ONE place it is allowed to refuse.

WHY IT IS NOT AT THE RECEIPT. ``inbound/receipt.py`` answers every rejection with
a byte-identical 403 (``_FORBIDDEN_BODY``), deliberately: a response that varied
with whether the sender is known would tell an attacker which addresses exist.
An allowlist enforced there would either break that constancy or refuse
invisibly. So the receipt keeps accepting and retaining what it can verify by
token, and the allowlist is applied where the bytes are IMPORTED -- where a
refusal becomes a durable ``REJECTED`` state naming its reason, readable by the
Datastream's owner and by nobody else.

WHY IT IS ENFORCED IN ``inbound_ingest``, WHICH IS NOT WHERE IT WAS FIRST PUT.
Two paths import inbound bytes: a delivery arriving (``inbound_processing``) and
a retained delivery being replayed (``inbound_reprocess``). The first version of
this policy guarded only the first, so a file received BEFORE a list was declared
could still be re-imported unchecked -- and that is exactly the case a list
exists for, since one declares it after seeing an arrival one did not want. A
list with a way around it is not a list. The check therefore sits at the point
both paths reach, ``ingest_inbound_file``, immediately beside the CHANNEL
allowlist that already lives there and reads the same ``config``; the replay path
needed no change at all to be covered, which is how one can tell it is the right
place.

WHY THE ADDRESS IS NEVER STORED IN THE MANIFEST. The manifest lives in the
quarantine store beside the delivered bytes, and ``receipt.py`` states its own
rule: "the raw routing token and the raw recipient address are NEVER written --
only their sha256 hashes". The sender follows that rule exactly, and gets the
same treatment as ``recipient_hash``: the manifest carries the digest of the
address and the digest of its domain, and this module hashes each declared entry
the same way to compare. A domain allowance therefore works without any address
ever being written down.

WHAT AN ABSENT DECLARATION MEANS. Nothing. A Datastream that declares no sender
is authorized exactly as it always was -- by the token carried in its address --
and every delivery passes. This module only ever narrows a promise somebody made.

Pure functions plus one read; the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The stable reason a delivery was refused after the fact. Named, because
#: "failed" would send the owner looking at the file instead of at the sender.
SENDER_NOT_ALLOWED = "sender_not_allowed"


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def sender_fingerprints(address: str | None) -> dict[str, str] | None:
    """Hash one sender address into the two forms an allowlist can match.

    ``{"sender_hash": ..., "sender_domain_hash": ...}``, or ``None`` when the
    transport carried no sender at all (a webhook has none).
    """
    if not isinstance(address, str):
        return None
    normalized = address.strip().lower()
    if not normalized or "@" not in normalized:
        return None
    domain = normalized.rsplit("@", 1)[1]
    if not domain:
        return None
    return {"sender_hash": _digest(normalized), "sender_domain_hash": _digest(domain)}


def allowed_senders_from_config(config: Any) -> list[str]:
    """The senders declared on one Datastream's own config. Never global."""
    if isinstance(config, (str, bytes, bytearray)):
        try:
            config = json.loads(config)
        except ValueError:
            return []
    if not isinstance(config, dict):
        return []
    contract = config.get("channel_contract")
    if not isinstance(contract, dict):
        return []
    declared = contract.get("allowed_senders")
    if not isinstance(declared, list):
        return []
    return [item.strip().lower() for item in declared if isinstance(item, str) and item.strip()]


def read_allowed_senders(conn, *, datastream_id: str) -> list[str]:
    """The senders this Datastream declared, read from its row."""
    with conn.cursor() as cur:
        cur.execute("SELECT config FROM app.datastreams WHERE id=%s", (datastream_id,))
        row = cur.fetchone()
    return allowed_senders_from_config(row[0]) if row else []


def read_delivery_sender(conn, *, raw_import_id: str) -> tuple[str | None, str | None]:
    """The sender digests recorded on the receipt these bytes arrived under.

    THE SAME JOIN `inbound_reprocess._origin_channel` already walks, and for the
    same reason: the receipt is the only durable evidence of a delivery that both
    import paths can read. The direct arrival still holds its manifest; a replay,
    hours or weeks later, has nothing but this row.

    `(None, None)` for a delivery with no recorded sender -- a webhook, or a
    receipt written before migration 214. Against a declared allowlist that is a
    refusal, never a waiver.
    """
    if not raw_import_id:
        return (None, None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rx.sender_hash, rx.sender_domain_hash "
            "FROM app.inbound_raw_imports r "
            "JOIN app.inbound_receipts rx ON rx.id = r.receipt_id "
            "WHERE r.id = %s",
            (raw_import_id,),
        )
        row = cur.fetchone()
    if not row:
        return (None, None)
    return (row[0] or None, row[1] or None)


def delivery_is_allowed(
    *, allowed: list[str], sender_hash: str | None, sender_domain_hash: str | None
) -> bool:
    """Does this delivery satisfy the declared allowlist?

    FAIL-CLOSED ON ABSENCE. When a list is declared and the delivery carries no
    sender, the answer is no: the operator asked for a restriction that cannot be
    checked, and letting it through would make the declaration decorative.
    """
    if not allowed:
        return True
    if not sender_hash and not sender_domain_hash:
        return False
    for entry in allowed:
        digest = _digest(entry)
        if "@" in entry:
            if sender_hash and digest == sender_hash:
                return True
        elif sender_domain_hash and digest == sender_domain_hash:
            return True
    return False
