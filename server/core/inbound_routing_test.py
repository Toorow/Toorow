"""Story 38.15 AC5 -- a Datastream-level synthetic test that proves ROUTING.

The connector already had a synthetic delivery: ``connector_verification.
run_synthetic_delivery``. It is scoped to (environment, connector_name), it is
platform-admin only, and it proves the receipt-adapter AUTHENTICATION seam. It
answers "is this connector's front door working at all?"

It does NOT answer the question a Datastream operator actually has before
telling a provider to start sending:

    "If a file is delivered to MY Datastream right now, does it reach it?"

That question is about the chain BEHIND the front door -- the Datastream's own
lifecycle, its configured channels, the verified domain, its delivery
credential, and whether that credential still resolves BACK to it. Every link
can be broken while the connector as a whole is perfectly healthy, and every one
of them fails the same way from outside: nothing arrives.

WHAT THIS PROVES, AND WHAT IT DOES NOT

It walks the exact gates a real delivery walks, in the order a real delivery
walks them, and it stops at the first one that refuses -- because a report that
kept going would list consequences of the first failure as if they were
independent problems.

It stops BEFORE anything is written. No receipt, no quarantine object, no raw
import, no mapping row, no execution, no publication. AC5 requires it to prove
routing "without publishing analytical rows"; writing nothing at all is the
strongest form of that, and it is also what keeps this indistinguishable-from-
nothing in the inbox: a synthetic test that appeared as a delivery would be a
phantom file an operator goes looking for.

IT SPENDS ONE RESOLUTION EVENT, DELIBERATELY

``resolve_by_token_hash`` records a rate-limit event. This module calls it
anyway, rather than re-deriving the same lookup while avoiding the throttle.

Re-deriving would duplicate security logic -- credential state, rotation
overlap, expiry, activation, Datastream binding, constant-shape denial -- into a
second copy that drifts silently. And the throttle is not incidental to routing:
a Datastream over its resolution budget REFUSES real deliveries, so a test that
routed around it would report "routing works" at the exact moment it does not.

The cost is one resolution event per manual test. That is the correct trade:
one code path, and a report that means what it says.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: The steps, in the order a real delivery meets them. The report is a list in
#: this order so a reader sees WHERE the chain breaks, not merely that it does.
STEP_DATASTREAM = "datastream_exists"
STEP_CONNECTOR = "connector_binding"
STEP_RECEIVABLE = "datastream_receivable"
STEP_DOMAIN = "domain_ready"
STEP_CREDENTIAL = "credential_active"
STEP_RESOLUTION = "credential_resolves_back"

ROUTING_STEPS: tuple[str, ...] = (
    STEP_DATASTREAM,
    STEP_CONNECTOR,
    STEP_RECEIVABLE,
    STEP_DOMAIN,
    STEP_CREDENTIAL,
    STEP_RESOLUTION,
)


class RoutingTestValidationError(ValueError):
    """The caller's arguments are unusable."""


def _step(name: str, *, passed: bool | None, reason: str | None = None, **extra) -> dict:
    """One link of the chain.

    ``passed`` is three-valued like every other health answer in this connector:
    None means NOT REACHED. A step after a failure is not "failing" -- nobody
    tried it -- and reporting it as False would send an operator to repair a
    link that may be perfectly fine.
    """
    out = {"step": name, "passed": passed, "reason": reason}
    out.update(extra)
    return out


def run_datastream_routing_test(
    conn,
    *,
    datastream_id: str,
    channel: str,
    environment: str | None = None,
) -> dict[str, Any]:
    """Walk the routing chain for one Datastream without writing anything.

    Returns ``{"synthetic": True, "routes": bool, "channel": ..., "steps": [...],
    "blocking_step": str | None, "blocking_reason": str | None}``.

    ``synthetic`` is stated in the payload and not left to the caller's memory:
    this result travels to a console banner and to an MCP host, and both must be
    able to say "this was a test" without knowing which endpoint produced it
    (38.15 AC5: "clearly distinguished from provider deliveries").

    No secret is returned. The credential is described by its state, version and
    ``safe_suffix`` -- never its hash, never its token.
    """
    from core.inbound_credentials import (  # noqa: PLC0415
        CREDENTIAL_CHANNELS,
        InboundCredentialDomainNotReady,
        InboundCredentialUnavailable,
        _effective_state,
        _get_datastream_status,
        _load_active_credential,
        _ready_domain,
        _require_receivable,
        resolve_by_token_hash,
    )

    datastream_id = (datastream_id or "").strip()
    channel = (channel or "").strip().lower()
    if not datastream_id:
        raise RoutingTestValidationError("datastream_id is required")
    if channel not in CREDENTIAL_CHANNELS:
        raise RoutingTestValidationError("unsupported delivery channel")
    environment = (
        environment or os.environ.get("TOOROW_ENVIRONMENT", "production")
    ).strip() or "production"

    steps: list[dict] = []

    def _finish() -> dict[str, Any]:
        # Fill the steps nobody reached, so the report has a fixed shape. A
        # report whose length varies with the failure is a report a screen has
        # to guess at.
        reached = {entry["step"] for entry in steps}
        for name in ROUTING_STEPS:
            if name not in reached:
                steps.append(_step(name, passed=None, reason="not_reached"))
        ordered = sorted(steps, key=lambda entry: ROUTING_STEPS.index(entry["step"]))
        failed = next((entry for entry in ordered if entry["passed"] is False), None)
        return {
            "synthetic": True,
            "datastream_id": datastream_id,
            "channel": channel,
            "routes": failed is None,
            "blocking_step": failed["step"] if failed else None,
            "blocking_reason": failed["reason"] if failed else None,
            "steps": ordered,
        }

    # --- 1. The Datastream ------------------------------------------------
    info = _get_datastream_status(conn, datastream_id=datastream_id)
    if info is None:
        steps.append(_step(STEP_DATASTREAM, passed=False, reason="datastream_not_found"))
        return _finish()
    steps.append(_step(STEP_DATASTREAM, passed=True))

    connector_name = info.get("connector_name") or ""
    if not connector_name:
        steps.append(_step(STEP_CONNECTOR, passed=False, reason="no_connector_bound"))
        return _finish()
    steps.append(_step(STEP_CONNECTOR, passed=True, connector_name=connector_name))

    # --- 2. Is it willing to receive on THIS channel? ---------------------
    try:
        _require_receivable(info, channel=channel)
    except InboundCredentialUnavailable as exc:
        # A CHANNEL THAT IS NOT CONFIGURED IS THE COMMON CASE, and it is
        # invisible from outside: the provider sends, and nothing ever arrives.
        steps.append(_step(STEP_RECEIVABLE, passed=False, reason=str(exc)))
        return _finish()
    steps.append(
        _step(
            STEP_RECEIVABLE,
            passed=True,
            lifecycle_state=info.get("lifecycle_state"),
            configured_channels=sorted(info.get("channels") or ()),
        )
    )

    # --- 3. The domain the provider would deliver to ----------------------
    try:
        _ready_domain(conn, connector_name=connector_name, environment=environment)
    except InboundCredentialDomainNotReady as exc:
        # The domain itself is NOT returned. It is platform-scoped
        # infrastructure, and a Datastream operator reading this report has no
        # authority over it -- naming it would invite a repair they cannot make.
        steps.append(_step(STEP_DOMAIN, passed=False, reason=str(exc)))
        return _finish()
    except Exception as exc:  # noqa: BLE001 -- unreadable is not unrouted
        logger.warning("routing test: domain unreadable: %s", type(exc).__name__)
        steps.append(_step(STEP_DOMAIN, passed=None, reason="domain_state_unreadable"))
        return _finish()
    steps.append(_step(STEP_DOMAIN, passed=True))

    # --- 4. A credential that is still valid ------------------------------
    credential = _load_active_credential(conn, datastream_id=datastream_id, channel=channel)
    if credential is None:
        steps.append(_step(STEP_CREDENTIAL, passed=False, reason="no_active_credential"))
        return _finish()
    effective = _effective_state(
        credential["state"],
        expires_at=credential.get("expires_at"),
        overlap_until=credential.get("overlap_until"),
    )
    if effective not in {"ACTIVE", "ROTATING"}:
        # STORED STATE IS NOT EFFECTIVE STATE. A row still marked ACTIVE whose
        # `expires_at` has passed is refused at delivery time, and an operator
        # reading the credential screen sees "ACTIVE" and concludes wrongly.
        steps.append(_step(STEP_CREDENTIAL, passed=False, reason="credential_not_effective"))
        return _finish()
    steps.append(
        _step(
            STEP_CREDENTIAL,
            passed=True,
            state=effective,
            version=credential.get("version"),
            safe_suffix=credential.get("safe_suffix"),
        )
    )

    # --- 5. Does it resolve BACK to this Datastream? ----------------------
    # The round trip is the point. Everything above can hold while the
    # credential resolves to a Datastream that was since disabled, or is refused
    # because the organization's activation lapsed -- and the constant-shape
    # denial means the provider learns nothing either way.
    try:
        resolution = resolve_by_token_hash(conn, token_hash=credential["token_hash"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("routing test: resolution unreadable: %s", type(exc).__name__)
        steps.append(_step(STEP_RESOLUTION, passed=None, reason="resolution_unreadable"))
        return _finish()

    scope = resolution.get("scope") or {}
    if not resolution.get("allowed"):
        # The denial is deliberately opaque -- that is AC4 of 38.7 and it is not
        # relaxed for a test. What we CAN say honestly is that it was refused.
        steps.append(_step(STEP_RESOLUTION, passed=False, reason="delivery_would_be_denied"))
        return _finish()
    if scope.get("datastream_id") != datastream_id:
        steps.append(_step(STEP_RESOLUTION, passed=False, reason="resolves_elsewhere"))
        return _finish()
    steps.append(_step(STEP_RESOLUTION, passed=True, resolved_channel=scope.get("channel")))

    return _finish()
