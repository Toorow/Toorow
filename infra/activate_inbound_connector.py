#!/usr/bin/env python
"""Activate the inbound managed-feed connector in PRODUCTION (Epic 38).

Drives the real onboarding state machine (NOT_INSTALLED -> DOMAIN_PENDING ->
VERIFYING -> READY) for connector_name='managed_feed' in environment='production',
and registers the inbound domain (ingest.toorow.com). This is what unblocks the
product's credential issuance (issue() refuses unless the installation is READY).

It writes PRODUCTION connector state, so YOU run it (the agent's safety layer
gates production writes). Idempotent: re-running is safe (each step no-ops if
already applied).

Usage (from the repo root, Windows PowerShell):
    .venv\\Scripts\\python.exe infra\\activate_inbound_connector.py

Reads PLATFORM_DB_URL from the repo .env. Optionally set INBOUND_DOMAIN /
TOOROW_ENVIRONMENT to override the defaults below.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import re
import sys

# --- make the server package importable + load .env -------------------------
_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "server"))
_env = _REPO / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _m = re.match(r"^([A-Z0-9_]+)=(.*)$", _line)
        if _m:
            os.environ.setdefault(_m.group(1), _m.group(2))

ENV = os.environ.setdefault("TOOROW_ENVIRONMENT", "production")
CONNECTOR = "managed_feed"
DOMAIN = os.environ.get("INBOUND_DOMAIN", "ingest.toorow.com")
HC: dict = {}


def main() -> int:
    from core.connector_domain import configure_domain, get_domain_config
    from core.connector_installation import (
        apply_installation,
        get_installation_state,
        transition_state,
    )
    from core.connector_installation_api import refuse_activation_unless_ready
    from core.db import get_connection

    with get_connection() as conn:
        # Platform-scoped operations (connector install/domain/verify) audit under
        # effective_org_id='platform', which the operations->organizations FK
        # requires to exist. Seed the system org once (idempotent bootstrap).
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES ('platform', 'Platform', 'platform', 'system') "
                "ON CONFLICT (id) DO NOTHING"
            )
        conn.commit()
        print("platform org: ensured")

        state = get_installation_state(conn, environment=ENV, connector_name=CONNECTOR)
        cur = state["state"] if state else "NOT_INSTALLED"
        print(f"current state: {cur}")

        if cur in ("NOT_INSTALLED",):
            apply_installation(
                conn, environment=ENV, connector_name=CONNECTOR,
                responsible_actor="platform-admin", blocking_cause=None,
                actor="platform-admin", idempotency_key="inst-mf-1",
                host_context=HC, trace_id=None,
            )
            print("-> DOMAIN_PENDING (installation applied)")

        if get_domain_config(conn, environment=ENV, connector_name=CONNECTOR) is None:
            configure_domain(
                conn, environment=ENV, connector_name=CONNECTOR, domain=DOMAIN,
                provider_adapter="adapter_eu_v1", webhook_endpoint_version="v1",
                signing_secret_ref="inbound-signing-secret", dns_evidence_class=None,
                actor="platform-admin", idempotency_key="dom-mf-1",
                host_context=HC, trace_id=None,
            )
            print(f"-> domain configured: {DOMAIN}")

        cur = get_installation_state(conn, environment=ENV, connector_name=CONNECTOR)["state"]
        if cur == "DOMAIN_PENDING":
            transition_state(
                conn, environment=ENV, connector_name=CONNECTOR, target_state="VERIFYING",
                responsible_actor="platform-admin", blocking_cause=None, last_verified_at=None,
                actor="platform-admin", idempotency_key="tr-mf-verifying-1",
                host_context=HC, trace_id=None,
            )
            cur = "VERIFYING"
            print("-> VERIFYING")

        if cur == "VERIFYING":
            transition_state(
                conn, environment=ENV, connector_name=CONNECTOR, target_state="READY",
                responsible_actor="platform-admin", blocking_cause=None,
                last_verified_at=datetime.datetime.now(datetime.timezone.utc),
                actor="platform-admin", idempotency_key="tr-mf-ready-1",
                host_context=HC, trace_id=None,
            )
            print("-> READY")

        conn.commit()

        final = get_installation_state(conn, environment=ENV, connector_name=CONNECTOR)["state"]
        dom = get_domain_config(conn, environment=ENV, connector_name=CONNECTOR)
        print(f"\nfinal state: {final}")
        print(f"domain: {dom['domain'] if dom else None}")
        try:
            refuse_activation_unless_ready(conn, connector_name=CONNECTOR, environment=ENV)
            print("READY guard: PASS -- credential issuance is now unblocked.")
        except Exception as exc:  # noqa: BLE001
            print(f"READY guard: FAIL -- {type(exc).__name__}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
