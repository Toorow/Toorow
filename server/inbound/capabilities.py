"""The named capability index `server/core` may ask of this package (AD-2).

``server/core`` must never name this package's provider modules -- that is the
whole of AD-2 / E38-NFR14, restated in ``inbound/__init__.py`` and in the
package README. Six core call sites named them anyway, and named them through
``importlib.import_module(f"inbound.{'adapters'}...")`` -- a split literal whose
ONLY effect is to slip past the static scanner in
``tests/inbound/test_source_agnostic_boundary.py``. The runtime behaviour of a
split literal is identical to a plain one; the split bought nothing but silence.

This index is the honest replacement. Core asks for a capability BY NAME and
never learns which module -- let alone which provider -- answers it. Rebinding a
capability to another adapter is an edit here, invisible to core, which is what
"provider is a deploy-time choice, never a code fork" was supposed to mean.

The values are module-level functions, not closures: nothing here captures a
connection or a request, so nothing here can leak one request's scope into the
next (the AI-125 class that killed the earlier `register_setup_adapter`
registry). Request-scoped adapters stay closures built by the caller, exactly as
`datastream_preconfiguration_api` builds them -- this index only hands out the
provider function those closures call.
"""

from __future__ import annotations

import importlib
from typing import Any

#: capability name -> (module, attribute). Core sees the KEYS only.
_CAPABILITIES: dict[str, tuple[str, str]] = {
    # Setup-time discovery adapters (Epic 47 / Story 57.1).
    "observe_connector_contract": (
        "inbound.adapters.datastream_setup",
        "observe_connector_contract",
    ),
    "observe_external_bigquery": (
        "inbound.adapters.datastream_setup",
        "observe_external_bigquery",
    ),
    "observe_staged_file": (
        "inbound.adapters.datastream_setup",
        "observe_staged_file",
    ),
    "observe_google_sheet": (
        "inbound.adapters.datastream_setup",
        "observe_google_sheet",
    ),
    "observe_channel_contract": (
        "inbound.adapters.datastream_setup",
        "observe_channel_contract",
    ),
    # Activation runtime (Epic 57 activation drivers + durable worker).
    "install_runtime_activation_drivers": (
        "inbound.adapters.datastream_activation_drivers",
        "install_runtime_activation_drivers",
    ),
    "execute_activation_job": (
        "inbound.datastream_activation_worker",
        "execute_activation_job",
    ),
    # Managed-feed values reader used by the hourly scheduled sync (Story 15.6).
    "managed_feed_values_adapter_factory": (
        "inbound.adapters.google_sheets_adapter",
        "google_sheets_values_adapter",
    ),
    # Setup-asset staging seam (Story 47.2).
    "stage_setup_asset": ("inbound.setup_assets", "stage_setup_asset"),
    "load_setup_asset": ("inbound.setup_assets", "load_setup_asset"),
    "SetupAssetNotFound": ("inbound.setup_assets", "SetupAssetNotFound"),
    "SetupAssetValidationError": ("inbound.setup_assets", "SetupAssetValidationError"),
}


class UnknownInboundCapability(KeyError):
    """Asked for a capability this package does not publish."""


def names() -> frozenset[str]:
    """Every capability core may ask for."""
    return frozenset(_CAPABILITIES)


def resolve(capability: str) -> Any:
    """Return the callable/class bound to ``capability``.

    Raises rather than returning ``None``: a capability that silently resolves to
    nothing reappears downstream as "the adapter produced no evidence", which is
    the failure mode `uncovered_adapter_evidence` exists to keep NAMED.
    """
    try:
        module_name, attribute = _CAPABILITIES[capability]
    except KeyError as exc:
        raise UnknownInboundCapability(
            f"{capability!r} is not published by inbound.capabilities; "
            f"known: {sorted(_CAPABILITIES)}"
        ) from exc
    # `import_module` is memoised by `sys.modules`; the deferred import keeps the
    # provider module (and its optional heavy client deps) off the import path of
    # every process that never uses it.
    return getattr(importlib.import_module(module_name), attribute)
