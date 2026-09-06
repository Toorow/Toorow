"""Which family a connector belongs to, and therefore what its installation owes.

ONE QUESTION, ONE DEFINITION (AI-206). Two steps of the installation lifecycle
depend on the same fact, and until this module existed each answered it its own
way:

  * `connector_installation_api` asked the module registry, correctly, and passed
    the answer to `apply_installation` as `requires_domain`;
  * `connector_verification.run_verification` could not ask anything -- the
    parameter was deliberately refused there, because a second hand-passed
    boolean could disagree with the first and the same connector would then owe a
    domain to one function and not to the other. So it read a PROXY instead:
    `current_state != "VERIFYING"`.

The proxy holds for exactly one instant. `apply_installation` opens a
domain-less installation at VERIFYING (migration 267), the first passing run
moves it to READY, and every later `POST /verify` was then refused for not
having a domain the connector never owed. Story 38.4 is titled CONTINUOUS
verification and its evidence row carries a `ttl_seconds`; the re-run that TTL
exists for could not happen for any of the 39 module connectors, and a platform
prerequisite that disappeared could never degrade the installation either --
READY is the only state `_DEGRADABLE` allows to leave, and READY was refused
entry.

The repair is not a third caller-supplied flag. It is this function: DERIVED, so
there is nothing to keep in step, and shared, so the two readers cannot diverge.

NOT STORED, deliberately. Which connectors ship as modules is a catalogue
delivered with the product -- a value that is derivable does not get a column
(CLAUDE.md, « Le code »). A column would also have to be back-filled for rows
written before it existed, and a wrong back-fill is exactly the silent
disagreement this module removes.

Source-agnostic (AD-2): no provider or vendor name appears here. The registry is
asked; no list is kept.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_module_connector(connector_name: str) -> bool:
    """Does this connector ship as a loaded module, rather than as a transport?

    A module is a Connector with a manifest, an ``auth`` block and a ``pull()``.
    The inbound family -- ``managed_feed`` -- is a transport, is not a module, and
    is the only thing ``app.connector_domain_configs`` was ever designed for:
    that table carries ``domain``, ``provider_adapter``,
    ``webhook_endpoint_version`` and ``dns_evidence_*``.

    Asked of the registry rather than of a list kept here: a list would be a
    second place to remember every time a Connector is added, and the registry
    already answers.
    """
    if not isinstance(connector_name, str) or not connector_name.strip():
        return False
    wanted = connector_name.strip()
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        return any(module.name == wanted for module in get_loaded_modules())
    except Exception:  # noqa: BLE001 -- an unreadable registry is not an answer
        logger.warning(
            "connector_family: module registry unreadable for %r"
            " -- treating the connector as a transport",
            connector_name,
        )
        return False


def owes_routing_contract(connector_name: str) -> bool:
    """Does an installation of this connector owe a domain routing contract?

    True for the inbound transport family, whose evidence IS a route: a domain was
    configured and a DNS record proved it. False for a module connector, which has
    no domain, no webhook and no DNS -- its verification evidence is an
    ``auth_check``, and migration 268 requires exactly that of a run carrying no
    routing contract.

    FAILS CLOSED. An unreadable registry answers True, which keeps the stricter
    path: a connector that stalls visibly is recoverable, one that skips a step it
    owed is not.
    """
    return not is_module_connector(connector_name)
