"""Cloudflare Email Worker inbound receipt adapter (Epic 38, Story 38.1).

Built as the SOURCE-AGNOSTIC SEAM PROOF (AC5) — it demonstrates that a second,
differently-shaped provider slots in behind the same ``ReceiptAdapter`` interface
without touching ``server/core``. It is NOT the shipped managed default:
Cloudflare Email Routing's ~5 MiB per-message ceiling is disqualifying for a file
connector whose scope includes large XLSX / .sav (SPSS) files (DECISION
2026-07-22). Self-hosted customers may still choose it via ``INBOUND_PROVIDER``.

Trust model: a customer-operated Cloudflare Email Worker receives the SMTP and
forwards a trusted POST carrying a shared secret in a header. We compare that
header to the configured secret with a constant-time compare BEFORE parsing.
"""

from __future__ import annotations

import hmac

from inbound.adapters import InboundDelivery, ReceiptRequest

# Header the trusted Worker sets to prove the forward is from the configured
# Worker and not an arbitrary internet client. Never logged.
_SHARED_SECRET_HEADER = "x-inbound-worker-secret"
# The Worker echoes its own delivery/message id here for idempotency.
_EVENT_ID_HEADER = "x-inbound-event-id"
_RECIPIENT_HEADER = "x-inbound-recipient"


class CloudflareWorkerAdapter:
    """Shared-secret header compare (constant time)."""

    def verify(self, request: ReceiptRequest) -> InboundDelivery | None:
        secret = request.signing_secret
        if not secret:
            return None

        # Header lookup is case-insensitive per the handler's normalization.
        presented = request.headers.get(_SHARED_SECRET_HEADER, "")
        if not presented:
            return None

        if not hmac.compare_digest(presented, secret):
            return None

        event_id = request.headers.get(_EVENT_ID_HEADER, "")
        recipient = request.headers.get(_RECIPIENT_HEADER, "") or request.form.get(
            "recipient", ""
        )
        if not event_id:
            # A verified forward MUST carry an idempotency id.
            return None

        attachments_meta = [
            {
                "name": f.get("name") or f.get("filename"),
                "size": f.get("size"),
                "content_type": f.get("content_type"),
            }
            for f in request.files
        ]

        return InboundDelivery(
            provider_event_id=event_id,
            recipient=recipient,
            attachments_meta=attachments_meta,
        )
