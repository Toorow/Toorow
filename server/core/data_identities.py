"""Stable Data object identities and immutable version evidence (Story 47.1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ulid import ULID

_CONTRACT_KEYS = (
    "name",
    "display_name",
    "schema_version",
    "module_kind",
    "auth_type",
    "account_topology",
    "report_profiles",
    "source_capabilities",
    "quota",
    "public_catalog",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def mint_data_id(prefix: str) -> str:
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("invalid data identity prefix")
    return f"{prefix}_{ULID()}"


def connector_contract_terms(manifest: dict[str, Any]) -> dict[str, Any]:
    """The subset of a manifest that IS the contract -- one definition, one place."""
    return {key: manifest[key] for key in _CONTRACT_KEYS if key in manifest}


def connector_contract_fingerprint(manifest: dict[str, Any]) -> str:
    """The fingerprint of the contract a module DECLARES right now, on disk.

    THE SAME FUNCTION THE WRITER USES, deliberately. The catalogue compares the
    module's current fingerprint against the one a binding pinned, and calls the
    difference `stale`. Computing it a second way here is how a pin silently
    stops matching a manifest that never changed.
    """
    return _sha256(connector_contract_terms(manifest))


def connector_contract_snapshot(
    manifest: dict[str, Any], *, validation_evidence: dict[str, Any]
) -> dict[str, Any]:
    """Return the safe canonical Connector contract persisted after validation."""
    if validation_evidence.get("status") != "validated":
        raise ValueError("connector contract must be validated before snapshotting")
    contract = connector_contract_terms(manifest)
    if not contract.get("name"):
        raise ValueError("connector contract requires a stable name")
    return {
        "schema_version": "1",
        "connector_id": str(contract["name"]),
        "contract": contract,
        "validation_evidence": validation_evidence,
        "connector_fingerprint": _sha256(contract),
    }


def record_connector_contract_snapshot(
    conn,
    *,
    environment: str,
    manifest: dict[str, Any],
    validation_evidence: dict[str, Any],
    verification_run_id: str | None,
    actor: str,
) -> dict[str, Any]:
    """Persist one immutable validated contract version, idempotent by fingerprint.

    A CONTRACT HANGS FROM ITS MODULE, in one environment (migration 248). It used
    to hang from an `app.connector_installations` row, whose lifecycle is an
    INBOUND CHANNEL's -- a domain to configure, a DNS route to verify. A pull
    Connector has none of those, so the only way to give it that parent was to
    declare it blocked on a domain that will never exist. Migration 247 made the
    wrong parent nullable; 248 removed it. An inbound contract loses nothing:
    `verification_run_id` still points at the run that proved its domain.
    """
    with conn.cursor() as cursor:
        return write_connector_contract_snapshot(
            cursor,
            environment=environment,
            manifest=manifest,
            validation_evidence=validation_evidence,
            verification_run_id=verification_run_id,
            actor=actor,
        )


def write_connector_contract_snapshot(
    cursor,
    *,
    environment: str,
    manifest: dict[str, Any],
    validation_evidence: dict[str, Any],
    verification_run_id: str | None,
    actor: str,
) -> dict[str, Any]:
    """Same write, on a cursor the caller already owns.

    The pin happens INSIDE the transaction that binds the Connector
    (`datastream_setup_observations._pin_connector_contract`), so it commits with
    the observation and the draft revision or with neither. A second connection
    would let a pin survive a binding that rolled back -- a contract version
    nothing points to, which is the class of row this whole repair removes.
    """
    snapshot = connector_contract_snapshot(manifest, validation_evidence=validation_evidence)
    version_id = mint_data_id("ccv")
    connector_id = snapshot["connector_id"]
    fingerprint = snapshot["connector_fingerprint"]
    cursor.execute(
        "SELECT COALESCE(MAX(version_number), 0) + 1 "
        "FROM app.connector_contract_versions "
        "WHERE connector_id = %s AND environment = %s",
        (connector_id, environment),
    )
    version_number = int((cursor.fetchone() or (1,))[0])
    cursor.execute(
        "INSERT INTO app.connector_contract_versions "
        "(id, environment, connector_id, version_number, "
        "contract_schema_version, connector_fingerprint, contract_snapshot, "
        "validation_evidence, verification_run_id, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
        "ON CONFLICT (connector_id, environment, connector_fingerprint) DO NOTHING",
        (
            version_id,
            environment,
            connector_id,
            version_number,
            str(manifest.get("schema_version") or "unknown"),
            fingerprint,
            _canonical_json(snapshot["contract"]),
            _canonical_json(validation_evidence),
            verification_run_id,
            actor,
        ),
    )
    cursor.execute(
        "SELECT id, version_number, connector_fingerprint "
        "FROM app.connector_contract_versions "
        "WHERE connector_id = %s AND environment = %s AND connector_fingerprint = %s",
        (connector_id, environment, fingerprint),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("connector contract snapshot was not persisted")
    return {
        "id": row[0],
        "version_number": int(row[1]),
        "connector_fingerprint": row[2],
    }


def build_event_configuration_version(
    *,
    event_configuration_id: str,
    datastream_id: str,
    version_number: int,
    connector_contract_version_id: str | None,
    connector_fingerprint: str | None,
    source_mapping: dict[str, Any],
    collection_policy: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Build content-addressed Event Configuration version evidence.

    A version is anchored to something IMMUTABLE, and there are two such things
    (Story 68.4). A Connector stream anchors to its contract version; a managed
    feed has no Connector -- `module_name IS NULL`, so no contract row can ever
    exist for it -- and anchors instead to the pinned MAPPING version, which the
    store already refuses to update (migration 032). Neither anchor is not an
    option: an unanchored version would claim to describe a collection whose
    shape nobody pinned.
    """
    mapping_anchor = str((source_mapping or {}).get("mapping_version_id") or "").strip()
    if not connector_contract_version_id and not mapping_anchor:
        raise ValueError(
            "an event configuration version needs an anchor: a connector "
            "contract version, or the pinned mapping version of a managed feed"
        )
    if connector_contract_version_id and len(connector_fingerprint or "") != 64:
        raise ValueError("connector contract version and fingerprint are required")
    if not event_configuration_id or not datastream_id or version_number < 1:
        raise ValueError("event configuration identity, Datastream and version are required")
    payload = {
        "event_configuration_id": event_configuration_id,
        "datastream_id": datastream_id,
        "version_number": version_number,
        "connector_contract_version_id": connector_contract_version_id,
        "connector_fingerprint": connector_fingerprint,
        "source_mapping": source_mapping,
        "collection_policy": collection_policy,
        "created_by": actor,
    }
    return {**payload, "normalized_payload_hash": _sha256(payload)}


def offline_contract_evidence(
    manifest: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Prove a pull Connector's contract WITHOUT a network call.

    THE GATE THAT WAS MISSING, and the reason the wizard's Connector list was
    empty in every deployment. The only writer of `app.connector_contract_versions`
    demanded a passing row in `app.connector_verification_runs` — a table whose
    verification is a DOMAIN verification: `connector_domain_configs` carries
    `domain`, `provider_adapter`, `webhook_endpoint_version`, `dns_evidence_*`.
    That gate belongs to an inbound channel. A `connector_pull` Connector has no
    domain, no webhook and no DNS to prove, so it could never pass it, so no
    contract was ever written, so the operator was offered nothing — for all 39.

    What a pull contract CAN prove offline is what it actually promises: the
    manifest validates its declared capabilities, it names report profiles, and
    every profile is structurally complete. That is the doctrine already held —
    prove the contract without a network before asking anyone for an account.

    Returns ``(passed, evidence)``. The evidence NEVER says `validated` unless
    every check held: `connector_contract_snapshot` refuses anything else, which
    keeps a failing check from being persisted as a contract.
    """
    from core.source_capabilities import validate_manifest_capabilities

    issues: list[str] = [str(issue) for issue in validate_manifest_capabilities(manifest)]

    name = str(manifest.get("name") or "").strip()
    if not name:
        issues.append("manifest declares no stable name")

    # A Connector with no report profile offers the wizard nothing to select at
    # step 2, so a contract for it would be a promise with no content.
    profiles = manifest.get("report_profiles") or manifest.get("reports") or []
    if not isinstance(profiles, list) or not profiles:
        issues.append("manifest declares no report profile")
    else:
        for index, profile in enumerate(profiles):
            named = isinstance(profile, dict) and str(
                profile.get("id") or profile.get("name") or ""
            ).strip()
            if not named:
                issues.append(f"report profile {index} has no id")

    passed = not issues
    return passed, {
        "status": "validated" if passed else "rejected",
        # Named, never blank: an operator reading this row must be able to tell
        # a structural proof from a live one and not mistake it for an account
        # that was actually reached.
        "evidence_class": "offline_contract_check",
        "checks": ["manifest_capabilities", "stable_name", "report_profiles"],
        "issues": issues,
    }


def snapshot_offline_connector_contract(
    conn,
    *,
    environment: str,
    manifest: dict[str, Any],
    actor: str,
) -> dict[str, Any] | None:
    """Pin a pull Connector's contract AT THE MOMENT IT IS BOUND, offline.

    WHEN THIS RUNS, and why it is not a seeding step. Nothing pre-writes a row
    per module: the catalogue is the registry (`context_seed.registry_module_names`)
    and needs no row to offer a Connector. A row appears the first time somebody
    actually binds that Connector to a Datastream, and it records what the module
    declared THEN -- which is what makes a later manifest change readable as
    `stale` instead of silently rewriting what a Datastream was built on.

    Returns None when the manifest does not pass — a Connector that cannot prove
    its own contract cannot be bound, and the step says so at the moment of the
    binding rather than hiding the Connector from the catalogue.
    """
    passed, evidence = offline_contract_evidence(manifest)
    if not passed:
        return None
    return record_connector_contract_snapshot(
        conn,
        environment=environment,
        manifest=manifest,
        validation_evidence=evidence,
        # No verification run: there was none. The column is nullable precisely
        # so a contract does not have to borrow someone else's evidence.
        verification_run_id=None,
        actor=actor,
    )


def snapshot_verified_connector_contract(
    conn,
    *,
    environment: str,
    connector_id: str,
    actor: str,
    modules_dir=None,
) -> dict[str, Any] | None:
    """Load a validated built-in manifest and pin it to the latest passing run.

    THE INBOUND PATH, and the one that legitimately has a verification run: a
    domain was configured and a DNS route was proven. The contract it writes
    still hangs from the MODULE (migration 248) -- the run is the evidence, and
    `verification_run_id` is where it is recorded. Nothing is lost by no longer
    naming the installation: the run points at it.
    """
    from pathlib import Path

    from core.source_capabilities import validate_manifest_capabilities

    root = (
        Path(modules_dir) if modules_dir is not None else Path(__file__).parent.parent / "modules"
    )
    matched_manifest: dict[str, Any] | None = None
    for manifest_path in sorted(root.glob("*/manifest.json")):
        candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
        if candidate.get("name") == connector_id:
            matched_manifest = candidate
            break
    if matched_manifest is None:
        return None
    issues = validate_manifest_capabilities(matched_manifest)
    if issues:
        raise ValueError("verified connector manifest does not pass capability validation")
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT r.id "
            "FROM app.connector_installations i "
            "JOIN app.connector_verification_runs r ON r.installation_id = i.id "
            "WHERE i.environment = %s AND i.connector_name = %s "
            "AND r.outcome = 'passed' "
            "ORDER BY r.created_at DESC LIMIT 1",
            (environment, connector_id),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError("connector contract snapshot requires passing verification evidence")
    return record_connector_contract_snapshot(
        conn,
        environment=environment,
        manifest=matched_manifest,
        validation_evidence={"status": "validated", "issues": []},
        verification_run_id=row[0],
        actor=actor,
    )
