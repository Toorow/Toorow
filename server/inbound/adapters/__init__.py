"""Provider-neutral receipt seam (Epic 38, Story 38.1, AC5).

The ``ReceiptAdapter`` Protocol is the extension point that keeps ``server/core``
provider-agnostic (AD-2 / E38-NFR14). Each concrete adapter (``mailgun``,
``cloudflare_worker``) verifies a delivery's signature/replay and normalizes it
into an :class:`InboundDelivery`. The adapter is chosen by the ``INBOUND_PROVIDER``
deploy-time env var — never hard-coded into the core.

Fail-closed contract: ``verify`` returns ``None`` for anything that is not a
signed delivery from the configured provider. The handler turns ``None`` into a
constant-shape 403 (no existence disclosure). No secret/token/capability is ever
returned in :class:`InboundDelivery` or logged (E38-NFR03).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Registry of known providers. Adding a provider here (source-agnostic seam) is
# the ONLY place provider vocabulary is allowed to live outside server/core.
_ADAPTER_REGISTRY = {
    "mailgun": ("inbound.adapters.mailgun", "MailgunAdapter"),
    "cloudflare_worker": ("inbound.adapters.cloudflare_worker", "CloudflareWorkerAdapter"),
}

DEFAULT_PROVIDER = "mailgun"


@dataclass(frozen=True)
class InboundDelivery:
    """Normalized, provider-neutral receipt envelope.

    Deliberately carries NO secret, NO raw body, and NO parsed content — only the
    minimum needed to bound + shape-check + acknowledge in Story 38.1. Token
    resolution to an ENABLED Datastream is Story 38.8, not here.

    Attributes:
        provider_event_id: The provider's idempotency/replay key (e.g. Mailgun
            ``token``). Used later for dedup; never a secret.
        recipient: The delivery recipient address (``ds_<token>@<domain>`` shape).
        attachments_meta: Per-attachment metadata (name/size/content-type) — NO
            attachment bytes. Used to enforce the attachment-count bound.
    """

    provider_event_id: str
    recipient: str
    attachments_meta: list[dict] = field(default_factory=list)


@runtime_checkable
class ReceiptAdapter(Protocol):
    """Verify a raw request and normalize it, or reject by returning ``None``.

    Implementations MUST verify signature + timestamp/replay BEFORE returning a
    delivery, and MUST use a constant-time comparison for any secret/HMAC check.
    """

    def verify(self, request: "ReceiptRequest") -> InboundDelivery | None:  # noqa: F821
        ...


@dataclass(frozen=True)
class ReceiptRequest:
    """Minimal, transport-neutral view of an inbound HTTP request for adapters.

    Kept framework-agnostic (no Starlette import here) so adapters stay pure and
    trivially unit-testable. The ASGI handler builds this from the live request.
    """

    headers: dict[str, str]
    form: dict[str, str]
    files: list[dict]
    signing_secret: str


def get_adapter(provider: str | None) -> ReceiptAdapter:
    """Resolve the configured adapter by name (default: mailgun).

    Raises:
        ValueError: if the provider is unknown. The caller must NOT leak this to
            the client — an unknown provider is an operator misconfiguration, not
            a request-shaped condition.
    """
    key = (provider or DEFAULT_PROVIDER).strip().lower()
    entry = _ADAPTER_REGISTRY.get(key)
    if entry is None:
        raise ValueError(f"unknown INBOUND_PROVIDER: {key!r}")
    module_name, class_name = entry
    import importlib

    module = importlib.import_module(module_name)
    adapter_cls = getattr(module, class_name)
    return adapter_cls()


__all__ = [
    "InboundDelivery",
    "ReceiptAdapter",
    "ReceiptRequest",
    "get_adapter",
    "DEFAULT_PROVIDER",
]
