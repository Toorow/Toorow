"""Mailgun inbound receipt adapter (Epic 38, Story 38.1).

Managed default (DECISION 2026-07-22, Jean: Mailgun EU). Mailgun signs each
inbound POST with an HMAC-SHA256 of ``timestamp`` + ``token`` against the
account signing key, sent as ``signature``. We verify that HMAC with a
constant-time compare BEFORE any body is parsed. Anything that fails returns
``None`` (the handler maps that to a constant-shape 403).

No secret/token is logged or returned in the normalized delivery (E38-NFR03).
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from inbound.adapters import InboundDelivery, ReceiptRequest

logger = logging.getLogger(__name__)

#: The three fields a signed delivery must carry. Their NAMES are safe to log;
#: their values are not.
_SIGNATURE_FIELDS = frozenset({"timestamp", "token", "signature"})

# Reject deliveries whose signed timestamp is older than this — replay window.
# Mailgun's own guidance is a few minutes; we keep it tight but tolerant of clock
# skew and provider->service latency.
_MAX_TIMESTAMP_SKEW_SECONDS = 15 * 60


class MailgunAdapter:
    """HMAC(timestamp + token) verification against the signing key."""

    def verify(self, request: ReceiptRequest) -> InboundDelivery | None:
        secret = request.signing_secret
        if not secret:
            return None

        form = request.form
        timestamp = form.get("timestamp", "")
        token = form.get("token", "")
        signature = form.get("signature", "")
        if not (timestamp and token and signature):
            # THE RESPONSE STAYS CONSTANT-SHAPE; THE LOG MUST NOT BE. Every
            # refusal answers the same 403 on purpose (E38-NFR03), and that is
            # right -- a caller must not learn which check failed. But the server
            # said nothing to ITSELF either, so a real delivery refused in
            # production was indistinguishable from a forged one, and diagnosing
            # it meant guessing. Measured 2026-08-07: a genuine Mailgun route
            # delivery was refused and the only evidence was `403 Forbidden`.
            #
            # Field NAMES only -- never a value, never the secret, never the
            # signature. Which keys arrived is not a secret; what they contain is.
            logger.warning(
                "mailgun: refused, signature fields missing -- present=%s",
                sorted(key for key in form if key in _SIGNATURE_FIELDS),
            )
            return None

        # Replay guard: reject stale timestamps before doing crypto.
        if not self._timestamp_fresh(timestamp):
            logger.warning("mailgun: refused, signed timestamp outside the replay window")
            return None

        expected = hmac.new(
            key=secret.encode("utf-8"),
            msg=f"{timestamp}{token}".encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

        # ``hmac.compare_digest`` raises TypeError on a non-ASCII str; an
        # attacker-controlled signature must fail closed (403), never crash us
        # into a distinguishable 500. A real hex HMAC is always ASCII.
        if not signature.isascii():
            return None
        # Constant-time compare — never a plain ``==`` on secrets/HMACs.
        if not hmac.compare_digest(expected, signature):
            logger.warning(
                "mailgun: refused, HMAC mismatch -- the three fields arrived and the "
                "digest does not match this deployment's signing key"
            )
            return None

        recipient = form.get("recipient", "")
        attachments_meta = self._attachments_meta(request)

        return InboundDelivery(
            provider_event_id=token,
            recipient=recipient,
            attachments_meta=attachments_meta,
            # Read only from a SIGNED delivery, like the recipient beside it, and
            # normalized no further here. It authorizes nothing: the token in the
            # recipient remains the only authorization. The handler hashes it
            # before anything durable is written.
            sender=form.get("sender", "") or form.get("from", ""),
        )

    @staticmethod
    def _timestamp_fresh(timestamp: str) -> bool:
        import time

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        now = int(time.time())
        # Reject future-dated (skew abuse) and stale timestamps symmetrically.
        return abs(now - ts) <= _MAX_TIMESTAMP_SKEW_SECONDS

    @staticmethod
    def _attachments_meta(request: ReceiptRequest) -> list[dict]:
        """Normalize attachment metadata (name/size/content-type) — no bytes."""
        meta: list[dict] = []
        for f in request.files:
            meta.append(
                {
                    "name": f.get("name") or f.get("filename"),
                    "size": f.get("size"),
                    "content_type": f.get("content_type"),
                }
            )
        return meta
