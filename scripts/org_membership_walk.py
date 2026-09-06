"""End-to-end walk of what the Members and Account-exposure sections drive.

    python scripts/disposable_postgres.py up --port 55520     # needs PG_BIN
    PLATFORM_DB_URL=postgresql://connector:connector_local_only@127.0.0.1:55520/toorow_test     TOOROW_INVITATION_PEPPER=<32 bytes> TOOROW_INVITATION_ORIGIN=https://console.example.com     python scripts/org_membership_walk.py

Never point PLATFORM_DB_URL at production: this walk WRITES (CLAUDE.md §6).
It is re-runnable — a fresh identity per run, because `_create_org` allows one
organization per person and answers 409 `organization_limit_reached` otherwise.

Not a unit test: it calls the REAL route handlers against a REAL PostgreSQL
cluster (disposable, 208 migrations from zero, non-superuser role), because a
stubbed `fetch` proves the contract and never proves that a removal reaches
`app.org_members`.

Each step prints what the server actually answered. A guard that is supposed to
refuse is asserted to refuse — a 409 that silently became a 200 is the failure
this walk exists to catch.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, "server")

# The handlers left `core.admin_api` when it split by responsibility. Bind each
# one from the module that owns it, so the next move fails HERE, at import,
# instead of rotting silently until line 96.
from types import SimpleNamespace  # noqa: E402

from core.credential_accounts_api import (  # noqa: E402
    _create_account_grant,
    _list_credential_grants,
    _register_credential_account,
    _revoke_account_grant,
)
from core.dataset_access_api import (  # noqa: E402
    _grant_dataset_access,
    _list_dataset_access_grants,
    _revoke_dataset_access,
)
from core.invitations_api import (  # noqa: E402
    _issue_invitation,
    _list_invitations,
    _revoke_invitation,
)
from core.org_members_api import (  # noqa: E402
    _list_org_members,
    _remove_org_member,
    _update_org_member,
)
from core.organizations_api import _create_org  # noqa: E402
from core.publication_reviews_api import _confirm_publication_review_console  # noqa: E402
from starlette.requests import Request  # noqa: E402

api = SimpleNamespace(
    _create_org=_create_org,
    _list_org_members=_list_org_members,
    _update_org_member=_update_org_member,
    _remove_org_member=_remove_org_member,
    _issue_invitation=_issue_invitation,
    _list_invitations=_list_invitations,
    _revoke_invitation=_revoke_invitation,
    _register_credential_account=_register_credential_account,
    _create_account_grant=_create_account_grant,
    _list_credential_grants=_list_credential_grants,
    _revoke_account_grant=_revoke_account_grant,
    _grant_dataset_access=_grant_dataset_access,
    _list_dataset_access_grants=_list_dataset_access_grants,
    _revoke_dataset_access=_revoke_dataset_access,
    _confirm_publication_review_console=_confirm_publication_review_console,
)

OK = "  ok "
BAD = "  XX "
failures: list[str] = []


def request(method: str, path: str, body=None, headers=None) -> Request:
    payload = json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"content-type", b"application/json")]
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http", "method": method, "path": path,
            "headers": hdrs, "query_string": b"", "path_params": {},
        },
        receive,
    )


def with_params(req: Request, **params) -> Request:
    req.scope["path_params"] = params
    return req


def check(label: str, got: int, want: int, note: str = "") -> dict:
    good = got == want
    print(f"{OK if good else BAD}{label}: HTTP {got} (expected {want}) {note}")
    if not good:
        failures.append(f"{label}: got {got}, expected {want}")
    return {}


def body_of(resp) -> dict:
    try:
        return json.loads(resp.body)
    except Exception:
        return {}


async def walk() -> None:
    # A fresh identity per run: `_create_org` enforces ONE org per person
    # (409 `organization_limit_reached`), so a re-used owner cannot create a
    # second walk org. That rule is the product working, not the walk failing.
    RUN = uuid.uuid4().hex[:8]
    OWNER = f"owner-{RUN}@example.com"
    SECOND = f"analyst-{RUN}@example.com"

    # Every call is made AS a named identity: the guards under test are the ones
    # that read who is calling. Every split module's `_check_auth` delegates AT
    # CALL TIME to `core.admin_api._check_auth`, so that seam is still the one
    # place to patch.
    import core.admin_api as admin_api  # noqa: PLC0415

    def as_identity(who: str):
        async def _check(_request):
            return True, who
        return patch.object(admin_api, "_check_auth", _check)

    print("\n--- organization and membership ---")
    with as_identity(OWNER):
        # A fresh slug per run: the org is unique-keyed on it, and a re-run that
        # collides returns 409 — which is how the existing suite was failing on a
        # cluster carrying residue, `KeyError: 'id'` and no reason shown.
        r = await api._create_org(request("POST", "/api/organizations",
                                          {"name": "Walk Org", "slug": f"walk-org-{uuid.uuid4().hex[:10]}"}))
        check("create org", r.status_code, 201)
        org = body_of(r)["id"]
        print(f"       org = {org}")

        r = await api._list_org_members(with_params(
            request("GET", "/x"), org_id=org))
        check("list members", r.status_code, 200)
        members = body_of(r).get("members", [])
        print(f"       members = {[(m['identity'], m['role']) for m in members]}")

    # The creator is auto-enrolled as owner. Add a second member directly so the
    # role/removal guards have something to act on that is not the last owner.
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, created_at) "
                "VALUES (%s, %s, %s, 'member', 'active', now())",
                (f"om_walk_{uuid.uuid4().hex[:12]}", org, SECOND),
            )
        conn.commit()
    print(f"{OK}seeded second member {SECOND}")

    print("\n--- invitations (the lifecycle the section now drives) ---")
    with as_identity(OWNER):
        r = await api._issue_invitation(with_params(
            request("POST", "/x",
                    {"invited_identity": f"newcomer-{RUN}@example.com", "role": "member",
                     "project_grants": [], "datastream_grants": [], "expires_in_hours": 48},
                    {"Idempotency-Key": f"walk-{RUN}-issue"}),
            org_id=org))
        check("issue invitation", r.status_code, 201, f"body={json.dumps(body_of(r))[:220]}")
        issued = body_of(r)
        invitation_id = issued.get("invitation_id")
        link = (issued.get("delivery_handoff") or {}).get("url")
        print(f"       invitation = {invitation_id}")
        print(f"       single-use link returned = {bool(link)}")
        if not link:
            failures.append("issue invitation: no delivery link — the section would show nothing to hand over")

        # The header the section always sends: without it the server refuses.
        r = await api._issue_invitation(with_params(
            request("POST", "/x",
                    {"invited_identity": f"other-{RUN}@example.com", "role": "member",
                     "project_grants": [], "datastream_grants": [], "expires_in_hours": 48}),
            org_id=org))
        check("issue without Idempotency-Key", r.status_code, 422,
              f"code={body_of(r).get('code')}")

        r = await api._list_invitations(with_params(request("GET", "/x"), org_id=org))
        check("list invitations", r.status_code, 200)
        items = body_of(r).get("items", [])
        print(f"       states = {[(i['state'], i['available_actions']) for i in items]}")

        if invitation_id:
            r = await api._revoke_invitation(with_params(
                request("POST", "/x", {}, {"Idempotency-Key": f"walk-{RUN}-revoke"}),
                org_id=org, invitation_id=invitation_id))
            check("revoke invitation", r.status_code, 200,
                  f"state={body_of(r).get('state')}")

    print("\n--- roles and removal, and the guards that must refuse ---")
    # The guard is the MANAGER floor (org_lifecycle._would_orphan_last_owner,
    # FIX 1b): the org must keep at least one ACTIVE owner-or-admin. While the
    # second member is a plain member, the owner is the sole active manager and
    # all three shapes must refuse.
    with as_identity(OWNER):
        r = await api._update_org_member(with_params(
            request("PATCH", "/x", {"role": "member"}), org_id=org, identity=OWNER))
        check("demote the sole active manager", r.status_code, 409,
              f"code={body_of(r).get('code')}")

        r = await api._update_org_member(with_params(
            request("PATCH", "/x", {"status": "suspended"}), org_id=org, identity=OWNER))
        check("suspend the sole active manager", r.status_code, 409,
              f"code={body_of(r).get('code')}")

        r = await api._remove_org_member(with_params(
            request("DELETE", "/x"), org_id=org, identity=OWNER))
        check("remove the sole active manager", r.status_code, 409,
              f"code={body_of(r).get('code')}")

        r = await api._update_org_member(with_params(
            request("PATCH", "/x", {"role": "admin"}), org_id=org, identity=SECOND))
        check("promote member -> admin", r.status_code, 200)

        # With a second active manager present, demoting the owner is ALLOWED —
        # that is the widening FIX 1b describes, and the walk asserted the old
        # owner-only guard until 2026-09-01.
        r = await api._update_org_member(with_params(
            request("PATCH", "/x", {"role": "member"}), org_id=org, identity=OWNER))
        check("demote the owner while an admin remains", r.status_code, 200)

    # No API path can restore the owner: `_enforce_role_assignment` (FIX 3)
    # forbids assigning ABOVE the actor's own rank, so the remaining admin
    # cannot mint an owner, and the ex-owner cannot self-escalate. Demoting the
    # last owner is a 200 that no 200 can undo — measured here, named in the
    # tracker. The walk asserts the refusal is real, then repairs its fixture
    # in SQL, which is exactly what a locked-out org would need an operator for.
    with as_identity(SECOND):
        r = await api._update_org_member(with_params(
            request("PATCH", "/x", {"role": "owner"}), org_id=org, identity=OWNER))
        check("the admin cannot mint an owner (above own rank)", r.status_code, 403,
              f"code={body_of(r).get('code')}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.org_members SET role = 'owner' "
                "WHERE org_id = %s AND identity = %s",
                (org, OWNER),
            )
        conn.commit()
    print(f"{OK}fixture repair: owner restored in SQL (no API path exists)")

    with as_identity(OWNER):
        r = await api._update_org_member(with_params(
            request("PATCH", "/x", {"role": "member"}), org_id=org, identity=SECOND))
        check("demote admin -> member", r.status_code, 200)

        # And the removal that IS allowed actually lands in the table.
        r = await api._remove_org_member(with_params(
            request("DELETE", "/x"), org_id=org, identity=SECOND))
        check("remove the second member", r.status_code, 200)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT identity, role FROM app.org_members WHERE org_id = %s ORDER BY identity",
                (org,))
            rows = cur.fetchall()
    print(f"       app.org_members now = {rows}")
    if any(r[0] == SECOND for r in rows):
        failures.append("removal returned 200 but the row is still in app.org_members")
    else:
        print(f"{OK}the removal reached the table, not just the response")
    if not any(r[0] == OWNER and r[1] == "owner" for r in rows):
        failures.append("the refused demotion still changed the owner's row")
    else:
        print(f"{OK}the three refusals left the owner's row untouched")

    print("\n--- account exposure (the outbound grant) ---")
    with as_identity(OWNER):
        # `connection_ref.project_id` is NOT NULL, so a credential needs a project
        # to hang off even though the GRANT itself is org-scoped (glossary.md:93).
        project = f"proj_walk_{org[-8:]}"
        cred = f"cred_walk_{org[-8:]}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.projects (id, org_id, name, slug, created_by, created_at) "
                    "VALUES (%s, %s, 'Walk Project', %s, %s, now())",
                    (project, org, f"walk-{org[-8:].lower()}", OWNER))
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, project_id, owner_org_id, owner_identity, "
                    " auth_path, nango_connection_id, created_at) "
                    "VALUES (%s, 'google_ads', %s, %s, %s, 'nango', %s, now())",
                    (cred, project, org, OWNER, f"nango_{cred}"))
            conn.commit()

        r = await api._register_credential_account(with_params(
            request("POST", "/x", {"external_account_id": "acct-walk-1", "label": "Account one"}),
            credential_id=cred))
        check("register provider account", r.status_code, 201)

        r = await api._create_account_grant(with_params(
            request("POST", "/x", {"grantee_org_id": org},
                    {"Idempotency-Key": f"walk-{RUN}-grant"}),
            credential_id=cred, external_account_id="acct-walk-1"))
        check("expose the account to this org", r.status_code, 201, f"body={json.dumps(body_of(r))[:250]}")

        r = await api._list_credential_grants(with_params(request("GET", "/x"), credential_id=cred))
        check("list grants", r.status_code, 200)
        grants = body_of(r).get("grants", [])
        print(f"       grants = {[(g['external_account_id'], g['grantee_org_id']) for g in grants]}")
        if not grants:
            failures.append("the grant returned 201 but the list is empty")

        r = await api._revoke_account_grant(with_params(
            request("DELETE", "/x"),
            credential_id=cred, external_account_id="acct-walk-1", grantee_org_id=org))
        check("revoke the exposure", r.status_code, 200)

    # -----------------------------------------------------------------------
    # Data access — who, outside toorow, may read the warehouse.
    #
    # The simulation `_simulate_bq_iam_grant` is gone: the grant is now a
    # two-phase operation whose provider half calls
    # `warehouse_tenancy.mutate_bigquery_dataset_access` for real. That GCP
    # boundary is patched here — same seam the unit suite patches — so the walk
    # still proves the PG half (rows, guards, idempotency) end to end.
    # -----------------------------------------------------------------------
    print("\n--- data access (external principals on the warehouse) ---")
    PRINCIPAL = "serviceAccount:walk@example.iam.gserviceaccount.com"
    import core.warehouse_tenancy as wt  # noqa: PLC0415

    provider_boundary = (
        patch.object(wt, "org_schemas_enabled", return_value=True),
        patch.object(wt, "bq_provisioning_enabled", return_value=True),
        patch.object(wt, "configured_bigquery_project", return_value="walk-project"),
        patch.object(
            wt,
            "resolve_org_schemas",
            return_value=wt.OrgSchemas(org, "walk", "walk", f"{org}_raw", f"{org}_marts"),
        ),
        patch.object(wt, "mutate_bigquery_dataset_access", return_value=None),
    )
    from contextlib import ExitStack  # noqa: PLC0415

    with ExitStack() as stack:
        for p in provider_boundary:
            stack.enter_context(p)
        stack.enter_context(as_identity(OWNER))
        # Every write here requires an Idempotency-Key — and each check needs a
        # FRESH one, or the second call is an idempotent replay of the first
        # instead of the refusal under test.
        r = await api._grant_dataset_access(with_params(
            request("POST", "/x", {"principal": PRINCIPAL},
                    {"Idempotency-Key": f"walk-{RUN}-da-grant"}), org_id=org))
        check("grant read access", r.status_code, 201, f"body={json.dumps(body_of(r))[:200]}")
        grant_id = body_of(r).get("id")

        # The validator the panel mirrors client-side. A malformed principal must
        # be refused by the SERVER too, or the client check is the only one.
        r = await api._grant_dataset_access(with_params(
            request("POST", "/x", {"principal": "not-a-principal"},
                    {"Idempotency-Key": f"walk-{RUN}-da-malformed"}), org_id=org))
        check("refuse a malformed principal", r.status_code, 422,
              f"code={body_of(r).get('code')}")

        # Granting the same principal twice must not create a second grant.
        r = await api._grant_dataset_access(with_params(
            request("POST", "/x", {"principal": PRINCIPAL},
                    {"Idempotency-Key": f"walk-{RUN}-da-duplicate"}), org_id=org))
        check("refuse a duplicate active grant", r.status_code, 409,
              f"code={body_of(r).get('code')}")

        r = await api._list_dataset_access_grants(with_params(request("GET", "/x"), org_id=org))
        check("list read access", r.status_code, 200)
        da = body_of(r).get("grants", [])
        print(f"       principals = {[g['principal'] for g in da]}")
        if len(da) != 1:
            failures.append(f"expected exactly one active grant, the list holds {len(da)}")

        # Revocation is BY GRANT ID, not by principal (`dataset_access_api.py#_revoke_dataset_access`).
        r = await api._revoke_dataset_access(with_params(
            request("DELETE", "/x", None, {"Idempotency-Key": f"walk-{RUN}-da-revoke"}),
            org_id=org, grant_id=grant_id))
        check("revoke read access", r.status_code, 200)

        # The list is an auditable ledger: a revoked grant STAYS listed, in state
        # 'revoked'. What must not remain is an ACTIVE row.
        r = await api._list_dataset_access_grants(with_params(request("GET", "/x"), org_id=org))
        left = [g for g in body_of(r).get("grants", [])
                if g["id"] == grant_id and g.get("lifecycle_state") != "revoked"]
        if left:
            failures.append("the revocation returned 200 but the grant is still active")
        else:
            print(f"{OK}the revocation reached the table, not just the response")

    # -----------------------------------------------------------------------
    # Publication review — NOT a capability walk. A measurement.
    #
    # `PublicationReviewModal.tsx` is imported by nothing at all, and it posts no
    # body. The confirm route demands a `confirmation_secret` that the modal has
    # no way to hold: the secret is minted server-side at prepare time and handed
    # over out of band, and no console surface prepares one. Adding the
    # `Idempotency-Key` it was missing was necessary and is not sufficient — this
    # step exists so that claim is measured rather than asserted.
    # -----------------------------------------------------------------------
    print("\n--- publication review (measuring what the orphaned modal would get) ---")
    with as_identity(OWNER):
        r = await api._confirm_publication_review_console(with_params(
            request("POST", "/x", {}, {"Idempotency-Key": f"walk-{RUN}-confirm"}),
            confirmation_id="conf_does_not_matter_here"))
        check("confirm with the header but no body", r.status_code, 422,
              f"code={body_of(r).get('code')}")
        if body_of(r).get("code") != "invalid_request":
            failures.append(
                "the confirm route no longer refuses a missing confirmation_secret — "
                "re-read PublicationReviewModal before trusting this walk")


asyncio.run(walk())

print("\n" + "=" * 62)
if failures:
    print(f"WALK FAILED — {len(failures)} step(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("WALK PASSED — every step reached a real PostgreSQL and every guard refused.")
