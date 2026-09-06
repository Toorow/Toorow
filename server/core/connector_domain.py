"""Story 38.3 -- domain and adapter-route configuration as versioned connector state.

A platform administrator configures the receiving domain and opaque delivery
adapter id for an installed connector in a given deployment environment. Every
configure write is versioned (append-only history), idempotent, and audited
end-to-end through the shared ``operations.execute_operation`` spine.

INVARIANTS (mirroring connector_installation.py, adversarially enforced):

  * SOURCE-AGNOSTIC. ``provider_adapter`` is referenced ONLY as an opaque id
    string. NO adapter/vendor vocabulary enters this module (AD-2). Even this
    docstring avoids naming adapters. The allow-list ``ALLOWED_PROVIDER_ADAPTERS``
    contains opaque id strings only -- the strings happen to identify adapters
    but the module treats them as an opaque closed set with no semantics attached.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * SECRET-FREE READ MODEL. ``signing_secret_ref`` is a reference id stored in
    the DB; it NEVER appears in any safe read-model, REST response, MCP response,
    log, or audit payload (E38-NFR03).

  * DETERMINISTIC IDEMPOTENCY (review H1). No random id enters
    ``request_payload``. Row ids are generated at WRITE TIME inside the mutation
    closure. Two identical configures with the same Idempotency-Key replay cleanly.

  * ROWCOUNT CHECKED (review H2). After ``INSERT ... ON CONFLICT DO NOTHING`` the
    rowcount is verified; on a lost race the call reconciles to the persisted row.
    The safe read-model is derived from ``op_result.result``, not a hard-coded
    target variable.

  * FAIL-CLOSED VALIDATION before any SQL: domain shape, provider adapter
    allow-list, installation existence and state, duplicate/cross-env conflict.

  * NO ``ds_<token>`` ISSUANCE. Datastream address tokens are 38.7 scope.

Mirrors ``connector_installation.py`` conventions: ``from __future__ import
annotations``, lazy imports of the shared seams, mutations only through the
operation seam, ASCII-only source.
"""

from __future__ import annotations

import re
from typing import Any

from ulid import ULID

from core.audit import declare_action
from core.operations import MutationResult, OperationSpec, execute_operation

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_CONNECTOR_DOMAIN_CONFIGURED = declare_action("connector.domain.configured")


# ---------------------------------------------------------------------------
# Constants (AC1, AC3, Task 2).
# ---------------------------------------------------------------------------

#: Opaque adapter id allow-list. These are string identifiers only -- this
#: module attaches NO business logic to them (AD-2). The set is the closed
#: authoritative list of ids this platform version recognises.
ALLOWED_PROVIDER_ADAPTERS: frozenset[str] = frozenset({
    "adapter_eu_v1",
    "adapter_us_v1",
    "adapter_global_v1",
    "adapter_eu_v2",
    "adapter_us_v2",
})

#: Domain configuration is a pre-verification operation. The installation row
#: is locked and must remain exactly DOMAIN_PENDING through the write.
_CONFIGURABLE_INSTALLATION_STATE = "DOMAIN_PENDING"

#: Simple domain shape: at least one dot, no scheme, no path, no port.
_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+$"
)
_SECRET_REF_RE = re.compile(
    r"^(?:secret-ref-[A-Za-z0-9_-]{1,200}|projects/[a-z0-9][a-z0-9-]{4,62}/"
    r"secrets/[A-Za-z0-9_-]{1,255}/versions/(?:[1-9][0-9]*|latest))$"
)
_EVIDENCE_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_WEBHOOK_VERSION_RE = re.compile(r"^v[1-9][0-9]{0,2}$")

# Safe next-action for a configured domain (no verification yet; 38.4 owns that).
_SAFE_NEXT_ACTION_CONFIGURED = (
    "platform_admin: trigger continuous verification to advance installation to VERIFYING"
)


# ---------------------------------------------------------------------------
# Exceptions (AC3, AC6, Task 2).
# ---------------------------------------------------------------------------


class ConnectorDomainValidationError(ValueError):
    """Raised before SQL when domain config inputs are unsafe or malformed."""


class ConnectorDomainConflict(RuntimeError):
    """The domain is already bound, or a cross-environment conflict exists.

    Maps to HTTP 409. Message is operator-facing and nondisclosing (no
    cross-tenant or secret detail).
    """


class ConnectorDomainUnavailable(RuntimeError):
    """The installation row does not exist or is not in a configurable state."""


# ---------------------------------------------------------------------------
# Safe read-model projection (AC6, Task 2). No secret, no cross-tenant detail.
# ---------------------------------------------------------------------------


def _safe_read_model(
    *,
    domain: str,
    provider_adapter: str,
    webhook_endpoint_version: str,
    dns_evidence_class: str | None,
    config_version: int,
    next_action: str,
    created_at: Any,
) -> dict[str, Any]:
    """Build the secret-free read-model projection for one domain config row.

    Fields returned:
      domain, provider_adapter, webhook_endpoint_version,
      dns_evidence_class, config_version, safe_next_action, configured_at.

    NEVER in this model: signing_secret_ref, dns_evidence_hash, installation_id,
    or any raw secret/credential (E38-NFR03, AC6).
    """
    cat: str | None = None
    if created_at is not None:
        try:
            cat = created_at.isoformat()
        except AttributeError:
            cat = str(created_at)
    return {
        "domain": domain,
        "provider_adapter": provider_adapter,
        "webhook_endpoint_version": webhook_endpoint_version,
        "dns_evidence_class": dns_evidence_class,
        "config_version": config_version,
        "safe_next_action": next_action,
        "configured_at": cat,
    }


# ---------------------------------------------------------------------------
# configure_domain -- versioned supersede + insert (AC1, AC4, Task 2).
# ---------------------------------------------------------------------------


def configure_domain(
    conn,
    *,
    environment: str,
    connector_name: str,
    domain: str,
    provider_adapter: str,
    webhook_endpoint_version: str,
    signing_secret_ref: str | None,
    dns_evidence_class: str | None,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    dns_evidence_hash: str | None = None,
) -> dict[str, Any]:
    """Configure the domain and adapter route for an installed connector.

    Versioned: supersedes the prior active config row (if any) and inserts a new
    one with config_version + 1. Idempotent replay on the same Idempotency-Key
    returns the existing config read-model without a new write. Conflicting payloads
    on the same key raise ``OperationIdempotencyConflict`` (409-mappable by the API).

    Fail-closed validation BEFORE SQL (review H1 / AC3):
      - domain shape must match ``_DOMAIN_RE``.
      - provider_adapter must be in ``ALLOWED_PROVIDER_ADAPTERS``.
      - The installation must exist and be exactly DOMAIN_PENDING.
      - Duplicate domain in this environment raises ``ConnectorDomainConflict``.
      - Domain already bound in a DIFFERENT environment raises
        ``ConnectorDomainConflict`` (cross-env guard, AC3).

    Does NOT issue a ``ds_<token>`` Datastream address (that is 38.7). Does NOT
    advance the installation state to VERIFYING (that is 38.4). The installation
    stays at DOMAIN_PENDING after a successful configure (AC5).

    Returns the safe read-model projection -- no secret, no cross-tenant detail.
    """
    # ------------------------------------------------------------------
    # Fail-closed validation (review H1: all checks before SQL).
    # ------------------------------------------------------------------
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorDomainValidationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorDomainValidationError("connector_name is required")
    if not isinstance(domain, str) or not domain.strip():
        raise ConnectorDomainValidationError("domain is required")
    if not isinstance(provider_adapter, str) or not provider_adapter.strip():
        raise ConnectorDomainValidationError("provider_adapter is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorDomainValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorDomainValidationError("idempotency_key is required")

    environment = environment.strip()
    connector_name = connector_name.strip()
    domain = domain.strip().lower()
    provider_adapter = provider_adapter.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()
    if len(idempotency_key) > 255:
        raise ConnectorDomainValidationError("idempotency_key is too long")
    if not isinstance(webhook_endpoint_version, str):
        raise ConnectorDomainValidationError("webhook_endpoint_version must be a string")
    webhook_endpoint_version = webhook_endpoint_version.strip() or "v1"
    if not _WEBHOOK_VERSION_RE.fullmatch(webhook_endpoint_version):
        raise ConnectorDomainValidationError("webhook_endpoint_version is invalid")
    if signing_secret_ref is not None:
        if not isinstance(signing_secret_ref, str) or not _SECRET_REF_RE.fullmatch(
            signing_secret_ref.strip()
        ):
            raise ConnectorDomainValidationError("signing_secret_ref must be a version reference")
        signing_secret_ref = signing_secret_ref.strip()
    if dns_evidence_class is not None:
        if not isinstance(dns_evidence_class, str) or not _EVIDENCE_CLASS_RE.fullmatch(
            dns_evidence_class.strip()
        ):
            raise ConnectorDomainValidationError("dns_evidence_class is invalid")
        dns_evidence_class = dns_evidence_class.strip()
    if dns_evidence_hash is not None:
        if not isinstance(dns_evidence_hash, str) or not _EVIDENCE_HASH_RE.fullmatch(
            dns_evidence_hash.strip().lower()
        ):
            raise ConnectorDomainValidationError("dns_evidence_hash must be a SHA-256 digest")
        dns_evidence_hash = dns_evidence_hash.strip().lower()

    if not _DOMAIN_RE.match(domain):
        raise ConnectorDomainValidationError(
            f"domain {domain!r} does not match the expected shape (no scheme, no port)"
        )

    if provider_adapter not in ALLOWED_PROVIDER_ADAPTERS:
        raise ConnectorDomainValidationError(
            f"provider_adapter {provider_adapter!r} is not in the allowed adapter set"
        )

    # ------------------------------------------------------------------
    # Verify installation existence and state BEFORE the operation lock.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s FOR UPDATE",
            (environment, connector_name),
        )
        inst_row = cur.fetchone()

    if inst_row is None:
        raise ConnectorDomainUnavailable(
            "connector installation row not found -- apply installation first"
        )

    installation_id, inst_state = inst_row
    if inst_state != _CONFIGURABLE_INSTALLATION_STATE:
        raise ConnectorDomainUnavailable(
            "connector installation is not available for domain configuration"
        )

    # ------------------------------------------------------------------
    # Cross-environment domain guard (AC3): reject if the same domain is
    # already ACTIVE in a DIFFERENT (environment, connector_name) pair.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT environment, connector_name "
            "FROM app.connector_domain_configs "
            "WHERE domain = %s AND superseded_by IS NULL",
            (domain,),
        )
        existing_domain_row = cur.fetchone()

    if existing_domain_row is not None:
        ex_env, ex_name = existing_domain_row
        # Same env + connector: duplicate configure -- we allow it (idempotent
        # re-configure bumps the version). The cross-env guard fires only when
        # a DIFFERENT binding exists.
        if not (ex_env == environment and ex_name == connector_name):
            raise ConnectorDomainConflict(
                "domain is already bound to another platform installation"
            )

    # ------------------------------------------------------------------
    # Load the current active config row (for versioning). FOR UPDATE
    # is inside the mutation closure to keep the lock duration minimal;
    # here we just read the current version number without locking.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, config_version "
            "FROM app.connector_domain_configs "
            "WHERE environment = %s AND connector_name = %s AND superseded_by IS NULL",
            (environment, connector_name),
        )
        prior_row = cur.fetchone()

    prior_id: str | None = None
    next_version: int = 1
    if prior_row is not None:
        prior_id, prior_version = prior_row
        next_version = prior_version + 1

    # ------------------------------------------------------------------
    # Build the OperationSpec (AC4: every write routes through execute_operation).
    # DETERMINISTIC PAYLOAD (review H1): no random id in request_payload.
    # The new row id is generated at write time INSIDE the mutation closure.
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type=ACTION_CONNECTOR_DOMAIN_CONFIGURED,
        actor=actor,
        effective_org_id=None,
        resource_path=(
            "platform:connector-domain-configs",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-domain-v1",
            "catalog": "connector-domain-v1",
            "tool": "rest-v1",
        },
        # No random id here -- deterministic over business inputs only (review H1).
        request_payload={
            "environment": environment,
            "connector_name": connector_name,
            "domain": domain,
            "provider_adapter": provider_adapter,
            "webhook_endpoint_version": webhook_endpoint_version,
            # signing_secret_ref is a reference id (safe to include as a ref);
            # the value is never included (E38-NFR03).
            "signing_secret_ref": signing_secret_ref,
            "dns_evidence_class": dns_evidence_class,
            "dns_evidence_hash": dns_evidence_hash,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-domain:{environment}:{connector_name}:configure"
        ),
        trace_id=trace_id,
    )

    def _mk_result(config_id: str, created_at: Any, version: int = next_version) -> MutationResult:
        from core.operations import _canonical_hash  # noqa: PLC0415

        result = {
            "config_id": config_id,
            "environment": environment,
            "connector_name": connector_name,
            "domain": domain,
            "provider_adapter": provider_adapter,
            "webhook_endpoint_version": webhook_endpoint_version,
            "dns_evidence_class": dns_evidence_class,
            "config_version": version,
            "configured_at": (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at)
                if created_at
                else None
            ),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            # Outbox: no secret, no signing_secret_ref value (E38-NFR03).
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "domain": domain,
                "provider_adapter": provider_adapter,
                "config_version": version,
            },
        )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        # Generate the new row id at write time (review H1: not hashed).
        new_config_id = f"cdc_{ULID()}"

        with operation_conn.cursor() as cur:
            # Supersede the prior active row if one exists (versioned history).
            if prior_id is not None:
                cur.execute(
                    "UPDATE app.connector_domain_configs "
                    "SET superseded_by = %s, updated_at = NOW() "
                    "WHERE id = %s AND superseded_by IS NULL",
                    (new_config_id, prior_id),
                )
                # If rowcount is 0, a concurrent configure won the race.
                if cur.rowcount != 1:
                    # Reconcile: reload the current active row.
                    cur.execute(
                        "SELECT id, domain, provider_adapter, webhook_endpoint_version, "
                        "dns_evidence_class, config_version, created_at "
                        "FROM app.connector_domain_configs "
                        "WHERE environment = %s AND connector_name = %s "
                        "AND superseded_by IS NULL",
                        (environment, connector_name),
                    )
                    race_row = cur.fetchone()
                    if race_row is None:  # pragma: no cover
                        raise ConnectorDomainConflict("configure race lost and row not found")
                    (
                        r_id, r_domain, r_adapter, r_wev,
                        r_dec, r_version, r_created,
                    ) = race_row
                    return _mk_result(r_id, r_created, r_version)

            # Insert the new versioned config row (review H2: rowcount checked).
            cur.execute(
                "INSERT INTO app.connector_domain_configs "
                "(id, installation_id, environment, connector_name, domain, "
                "provider_adapter, webhook_endpoint_version, signing_secret_ref, "
                "dns_evidence_class, dns_evidence_hash, config_version, "
                "superseded_by, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    new_config_id,
                    installation_id,
                    environment,
                    connector_name,
                    domain,
                    provider_adapter,
                    webhook_endpoint_version,
                    signing_secret_ref,
                    dns_evidence_class,
                    dns_evidence_hash,
                    next_version,
                    operation_id,
                ),
            )
            if cur.rowcount == 1:
                # Inserted successfully: read back the created_at timestamp.
                cur.execute(
                    "SELECT created_at FROM app.connector_domain_configs WHERE id = %s",
                    (new_config_id,),
                )
                ts_row = cur.fetchone()
                created_at = ts_row[0] if ts_row else None
                return _mk_result(new_config_id, created_at)

            # ON CONFLICT DO NOTHING: a concurrent insert won the race (review H2).
            cur.execute(
                "SELECT id, domain, provider_adapter, webhook_endpoint_version, "
                "dns_evidence_class, config_version, created_at "
                "FROM app.connector_domain_configs "
                "WHERE environment = %s AND connector_name = %s AND superseded_by IS NULL",
                (environment, connector_name),
            )
            race_row = cur.fetchone()
            if race_row is None:  # pragma: no cover
                raise ConnectorDomainConflict("insert race lost and row not found")
            (
                r_id, r_domain, r_adapter, r_wev,
                r_dec, r_version, r_created,
            ) = race_row
            return _mk_result(r_id, r_created, r_version)

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(
        domain=data.get("domain", domain),
        provider_adapter=data.get("provider_adapter", provider_adapter),
        webhook_endpoint_version=data.get("webhook_endpoint_version", webhook_endpoint_version),
        dns_evidence_class=data.get("dns_evidence_class", dns_evidence_class),
        config_version=data.get("config_version", next_version),
        next_action=_SAFE_NEXT_ACTION_CONFIGURED,
        created_at=data.get("configured_at"),
    )


# ---------------------------------------------------------------------------
# get_domain_config -- nondisclosing read (AC6, Task 2).
# ---------------------------------------------------------------------------


def get_domain_config(
    conn,
    *,
    environment: str,
    connector_name: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for the active domain config, or None if absent.

    None means no domain has been configured for this connector in this
    environment. Callers treat None as unconfigured for display purposes.
    Does NOT raise for absence; existence is never disclosed to unauthorized
    callers (that is the REST layer's responsibility).

    NEVER returns signing_secret_ref, dns_evidence_hash, or any raw secret
    material (AC6, E38-NFR03).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT domain, provider_adapter, webhook_endpoint_version, "
            "dns_evidence_class, config_version, created_at "
            "FROM app.connector_domain_configs "
            "WHERE environment = %s AND connector_name = %s "
            "AND superseded_by IS NULL",
            (environment, connector_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    (
        domain,
        provider_adapter,
        webhook_endpoint_version,
        dns_evidence_class,
        config_version,
        created_at,
    ) = row
    return _safe_read_model(
        domain=domain,
        provider_adapter=provider_adapter,
        webhook_endpoint_version=webhook_endpoint_version,
        dns_evidence_class=dns_evidence_class,
        config_version=config_version,
        next_action=_SAFE_NEXT_ACTION_CONFIGURED,
        created_at=created_at,
    )
