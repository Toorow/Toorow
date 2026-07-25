"""toorow inbound receipt spine (Epic 38, Story 38.1).

The internet-facing, scale-to-zero entry point for inbound Email/File provider
deliveries. This package is SOURCE-AGNOSTIC by construction: provider vocabulary
(Mailgun, Cloudflare, ...) lives ONLY in ``inbound.adapters`` and is selected by
deploy-time config (``INBOUND_PROVIDER``). ``server/core`` must never import this
package's provider names (AD-2 / E38-NFR14).

Story 38.1 is the RECEIPT SPINE ONLY: verify -> bound -> shape-check -> 202. It
writes NOTHING durable (no quarantine, no DB, no parsing). Those are 38.6+.
"""

from __future__ import annotations

from inbound.adapters import InboundDelivery, ReceiptAdapter, get_adapter

__all__ = ["InboundDelivery", "ReceiptAdapter", "get_adapter"]
