"""Erasing an organization, and who is left owning what.

AD-43, 2026-08-12. These six functions execute SQL, mutate durable state and are
mounted by NOTHING -- they are a service, and they were living in the route file.
`module-boundaries.md` criterion 4 has said so since the day it was written; what
was missing was the count. A **244-line transaction that erases an organization**
is the whole of that criterion in one function.

WHY THIS MOVE COULD NOT REGRESS A ROUTE. Not one of them is mounted, so the
`Router` dump is identical by construction. That is the cheapest proof available
and the reason the services leave before the address families do.

WHAT STAYS BEHIND. The auth and scope seam -- 243 lines of it -- keeps living in
`admin_api`: authorization IS a route module's job. A service answers "what does
erasing this organization touch", never "may this caller ask".
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse, Response

from core.audit import (
    ACTION_DATASET_ACCESS_REVOKED,
    ACTION_ORG_SCHEMAS_DROPPED,
    declare_action,
)

# --- LES ACTIONS QUE CE MODULE ECRIT (AD-42) -----------------------------
#
# `org_deleted` a suivi son ecrivain ici. `dataset_access.revoked` a DEUX
# ecrivains -- ce service et la porte de partage de dataset -- donc elle reste
# declaree au centre, ce que la regle prevoit pour un vocabulaire partage.
ACTION_ORG_DELETED = declare_action("org_deleted")

logger = logging.getLogger("core.admin_api")

#: Direct org-scoped dependencies surfaced to a human before the drop. Only the
#: tables someone recognises by name: the FULL tenant tree (~40 tables) is walked
#: by core.org_purge and reported as `purged_tables` in the org_deleted audit row.
#: Every one of these is a table whose FK into app.organizations is RESTRICT (or
#: CASCADE for members) -- i.e. exactly what used to make DELETE fail with 409.
_ORG_DEPENDENT_COUNTS: tuple[tuple[str, str], ...] = (
    ("datastreams", "SELECT count(*) FROM app.datastreams WHERE org_id = %s"),
    ("connections", "SELECT count(*) FROM app.connection_ref WHERE owner_org_id = %s"),
    ("invitations", "SELECT count(*) FROM app.invitations WHERE org_id = %s"),
    ("members", "SELECT count(*) FROM app.org_members WHERE org_id = %s"),
    ("operations", "SELECT count(*) FROM app.operations WHERE effective_org_id = %s"),
)

_MEMBERSHIP_FACTS_SQL = """
    SELECT m.org_id, o.name, o.slug, m.role, m.status,
           (SELECT count(*) FROM app.org_members x
             WHERE x.org_id = m.org_id AND x.identity <> %s AND x.status = 'active'),
           (SELECT count(*) FROM app.org_members y
             WHERE y.org_id = m.org_id AND y.identity <> %s
               AND y.status = 'active' AND y.role = 'owner')
    FROM app.org_members m
    JOIN app.organizations o ON o.id = m.org_id
    WHERE m.identity = %s
    ORDER BY o.name, m.org_id
"""

def _erase_org_transactional(
    pg_conn,
    org_id: str,
    *,
    name: str,
    slug: str,
    identity: str,
) -> tuple[dict | None, Response | None]:
    """Erase org *org_id* on the caller's OPEN transaction -- no commit here.

    Returns ``(result, None)`` when the erasure is staged and only a commit is
    missing, or ``(None, response)`` when it was refused/failed -- in which case
    the transaction has already been rolled back and the org is fully intact.

    Dismantling order (the whole point of this function -- each step exists
    because the previous one cannot succeed without it):

      1. refuse on ACTIVE projects (409): a live project is a human decision,
         never something a delete endpoint resolves on its own;
      2. snapshot the facts + the ACTIVE dataset grants BEFORE touching a row --
         they are about to be erased and become unobservable;
      3. purge the tenant tree (core.org_purge): cycle-breaking UPDATEs first,
         then DELETEs deepest-first over the RESTRICT/NO-ACTION edges. This is
         what unpins the ~13 tables that used to answer 409 "conflict";
      4. DELETE the org row -- still uncommitted. Anything the purge missed
         surfaces HERE as an FK violation naming its own table (409), never as
         a silent partial erasure;
      5. drop the warehouse datasets (external, non-transactional) only once
         Postgres has proven deletable. If the drop cannot be confirmed we
         rollback: the row survives, RGPD invariant intact (never the reverse
         order -- dropping data whose row then survives is unrecoverable);
      6. write the audit rows on this same transaction. The caller commits, so
         evidence and effect commit together or not at all.
    """
    ident = identity or "anonymous"

    with pg_conn.cursor() as cur:
        # (1) Pre-check: block if active projects exist (readable 409).
        # The FK ON DELETE RESTRICT is the authoritative truth; this check
        # gives a human-readable error before we attempt the DELETE.
        cur.execute(
            "SELECT 1 FROM app.projects WHERE org_id = %s AND status != 'archived' LIMIT 1",
            (org_id,),
        )
        if cur.fetchone() is not None:
            pg_conn.rollback()
            return None, JSONResponse(
                {
                    "code": "org_has_active_projects",
                    "message": ("Archive all active projects before deleting the organization."),
                },
                status_code=409,
            )

        # (2) Epic-24 review X-1/F-2b: snapshot ACTIVE dataset-access grants
        # BEFORE the DELETE -- the 047 FK ON DELETE CASCADE erases the rows
        # in this same transaction (RGPD erasure by design); the durable
        # trace is the audit entry emitted per grant in phase 3.
        cur.execute(
            "SELECT id, principal FROM app.dataset_access_grants "
            "WHERE org_id = %s AND revoked_at IS NULL",
            (org_id,),
        )
        active_grants = cur.fetchall()

    # Same snapshot the preview shows, taken inside the erasing transaction so
    # the reported `removed` counts are the rows actually about to go (members
    # and grants leave via CASCADE and are unobservable afterwards).
    facts = _org_deletion_facts(pg_conn, org_id, name=name, slug=slug)

    with pg_conn.cursor() as cur:
        # (3) Erase the tenant tree before the org row. ~50 ON DELETE RESTRICT
        # foreign keys pin it in place (org -> projects -> datastreams ->
        # ...), and those RESTRICT rules are wanted everywhere else, so the
        # erasure is explicit here instead of being made implicit in the
        # schema. Runs on THIS connection, inside THIS transaction: nothing
        # is committed until the org row itself is gone.
        from core.org_purge import purge_org_tree  # noqa: PLC0415

        try:
            purge_result = purge_org_tree(pg_conn, org_id)
        except Exception:
            pg_conn.rollback()
            logger.exception("admin_api: delete_org purge_failed org=%s", org_id)
            return None, JSONResponse(
                {
                    "code": "purge_failed",
                    "message": (
                        "Dependent records could not be erased; the "
                        "organization was NOT deleted. Investigate and retry."
                    ),
                },
                status_code=500,
            )

        # (4) Issue the DELETE inside the still-open transaction (no commit yet).
        # If a project slipped through between the pre-check and now, the FK
        # ON DELETE RESTRICT raises here -> we rollback -> 409 (no partial drop).
        try:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        except Exception as exc:
            # Name the actual blocker. The previous generic "conflict" sent
            # operators hunting for an active project when the real holder
            # was something else entirely (an archived project's rows, a
            # connection, ...): the pre-check above only covers NON-archived
            # projects, while the FK restricts on every referencing row.
            diag = getattr(exc, "diag", None)
            blocking_table = getattr(diag, "table_name", None)
            blocking_constraint = getattr(diag, "constraint_name", None)
            pg_conn.rollback()
            logger.warning(
                "admin_api: delete_org pg_delete_fk_violation org=%s table=%s constraint=%s",
                org_id,
                blocking_table,
                blocking_constraint,
            )
            detail = (
                f" Still referenced by {blocking_table} ({blocking_constraint})."
                if blocking_table
                else ""
            )
            return None, JSONResponse(
                {
                    "code": "conflict",
                    "message": ("Organization could not be deleted due to a conflict." + detail),
                    "blocking_table": blocking_table,
                    "blocking_constraint": blocking_constraint,
                },
                status_code=409,
            )

    # (5) Drop warehouse schemas BEFORE committing the Postgres DELETE.
    # If the drop fails we rollback -> org row stays intact (RGPD invariant).
    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    try:
        drop_result = _wt.drop_org_schemas(org_id=org_id, conn=None)
    except Exception:
        try:
            pg_conn.rollback()
        except Exception:
            pass
        logger.exception("admin_api: delete_org schema_drop_failed org=%s", org_id)
        return None, JSONResponse(
            {
                "code": "schema_drop_failed",
                "message": (
                    "Warehouse schema drop failed; Postgres record NOT deleted "
                    "(RGPD safety). Investigate and retry."
                ),
            },
            status_code=500,
        )

    drop_status = drop_result.get("status")
    drop_reason = drop_result.get("reason", "")

    # "skipped / unresolvable" -> cannot confirm data removal -> block (RGPD).
    # "skipped / no_duckdb_path" -> nothing to drop -> proceed.
    if drop_status == "skipped" and drop_reason == "unresolvable":
        try:
            pg_conn.rollback()
        except Exception:
            pass
        logger.error(
            "admin_api: delete_org slug_unresolvable org=%s -- blocking deletion",
            org_id,
        )
        return None, JSONResponse(
            {"code": "schema_drop_failed", "message": "warehouse operation failed"},
            status_code=500,
        )

    # (6) Audit rows on the same transaction; the CALLER commits.
    from core.audit import insert_audit_row  # noqa: PLC0415

    # Emit org_schemas_dropped only when a real drop occurred (F-7).
    if drop_status == "ok":
        insert_audit_row(
            pg_conn,
            identity=ident,
            action=ACTION_ORG_SCHEMAS_DROPPED,
            provider_account="",
            connection_ref="",
            metadata={
                "org_id": org_id,
                "raw": drop_result.get("raw"),
                "marts": drop_result.get("marts"),
                "drop_status": drop_status,
            },
        )
    # Epic-24 review X-1/F-2b: one revoke audit per grant erased by the 047
    # CASCADE -- the audit table is the durable RGPD trace of the exposure
    # gesture; without this an active grant would vanish untracked.
    for grant_id, principal in active_grants:
        insert_audit_row(
            pg_conn,
            identity=ident,
            action=ACTION_DATASET_ACCESS_REVOKED,
            provider_account="",
            connection_ref="",
            metadata={
                "org_id": org_id,
                "grant_id": grant_id,
                "principal": principal,
                "reason": "org_deleted",
            },
        )

    # What the caller reports back, and what the audit records: the same dict.
    removed = {
        "projects": len(facts["projects"]),
        **facts["counts"],
        # Everything else the purge erased across the tenant tree, so the total
        # is not silently reduced to the five human-readable buckets.
        "tenant_rows": purge_result.get("total_rows", 0),
    }
    insert_audit_row(
        pg_conn,
        identity=ident,
        action=ACTION_ORG_DELETED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "slug": slug,
            "name": name,
            "drop_status": drop_status,
            "revoked_grants": len(active_grants),
            # Durable proof of what the erasure actually removed: the rows
            # themselves are gone, so this is the only remaining evidence.
            "purged_rows": purge_result.get("total_rows", 0),
            "purged_tables": purge_result.get("rows_by_table", {}),
            "removed": removed,
        },
    )
    return (
        {
            "removed": removed,
            "drop_status": drop_status,
            "warehouse_datasets": facts["warehouse_datasets"],
        },
        None,
    )

def _org_deletion_facts(conn, org_id: str, *, name: str, slug: str) -> dict:
    """What deleting *org_id* would remove, and what would stop it.

    Read-only. Runs on the CALLER's connection so the erasure path can take this
    snapshot inside its own transaction (the counts then describe exactly the
    rows it is about to erase -- no TOCTOU between preview and deletion).

    ``blockers`` empty == the org is deletable. A blocker is never worked
    around: it names a dependency that cannot legitimately be erased here.
    """
    projects: list[dict] = []
    counts: dict[str, int] = {}
    blockers: list[dict] = []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, status FROM app.projects WHERE org_id = %s ORDER BY name, id",
            (org_id,),
        )
        for pid, pname, pstatus in cur.fetchall() or []:
            projects.append({"id": pid, "name": pname, "status": pstatus})

        for key, sql in _ORG_DEPENDENT_COUNTS:
            # A count is diagnostic, never authoritative (the FKs are). A table
            # missing from an older deployment must therefore not abort the
            # caller's transaction, hence one savepoint per probe.
            cur.execute("SAVEPOINT org_facts")
            try:
                cur.execute(sql, (org_id,))
                row = cur.fetchone()
                counts[key] = int(row[0]) if row else 0
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT org_facts")
                logger.warning(
                    "admin_api: org_deletion_facts count_failed org=%s key=%s",
                    org_id,
                    key,
                )
                counts[key] = 0
            else:
                cur.execute("RELEASE SAVEPOINT org_facts")

    active = [p for p in projects if p["status"] != "archived"]
    if active:
        blockers.append(
            {
                "kind": "active_projects",
                "detail": (
                    f"{len(active)} active project(s) still attached: "
                    + ", ".join(p["name"] for p in active[:5])
                    + ". Archive or delete them before deleting the organization."
                ),
            }
        )

    # Warehouse: the datasets provisioned at org creation. If the topology
    # cannot be resolved, drop_org_schemas returns "unresolvable" and the
    # erasure blocks (RGPD: never report an erasure we could not perform), so
    # the preview must announce that up front rather than let the DELETE 500.
    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    schemas = _wt.resolve_org_schemas(org_id=org_id, conn=conn, fresh=True)
    datasets = [schemas.raw, schemas.marts] if schemas is not None else []
    if schemas is None:
        blockers.append(
            {
                "kind": "warehouse_unresolvable",
                "detail": (
                    "Warehouse datasets cannot be resolved for this organization, "
                    "so their removal cannot be confirmed. Deletion is blocked "
                    "until the topology resolves."
                ),
            }
        )

    return {
        "org_id": org_id,
        "name": name,
        "slug": slug,
        "projects": projects,
        "counts": counts,
        "warehouse_datasets": datasets,
        "blockers": blockers,
    }

def _account_deletion_facts(conn, identity: str) -> dict:
    """What erasing *identity* would do, and what would stop it. Read-only.

    ``sole_owner_of`` is every org where the caller is the ONLY active owner --
    i.e. every org whose fate depends on this account. Each of them either
    leaves with the account (nobody else is active in it) or blocks the erasure
    (someone else is), and ``blockers`` says which, per org, with the remedy.
    """
    from core.user_profiles import fetch_user_profile  # noqa: PLC0415

    profile = fetch_user_profile(identity, conn)

    memberships: list[dict] = []
    sole_owner_of: list[dict] = []
    blockers: list[dict] = []
    orgs_to_erase: list[dict] = []

    with conn.cursor() as cur:
        cur.execute(_MEMBERSHIP_FACTS_SQL, (identity, identity, identity))
        rows = cur.fetchall() or []

    for org_id, name, slug, role, status, others, other_owners in rows:
        memberships.append(
            {
                "org_id": org_id,
                "org_name": name,
                "role": role,
                "other_active_members": int(others or 0),
            }
        )
        # Only an ACTIVE owner owns anything: an invited/suspended row holds no
        # organization hostage, so it never blocks and never drags an org along.
        if role != "owner" or status != "active" or int(other_owners or 0) > 0:
            continue
        sole_owner_of.append({"org_id": org_id, "org_name": name})
        if int(others or 0) > 0:
            blockers.append(
                {
                    "kind": "sole_owner_with_members",
                    "detail": (
                        f'You are the only owner of "{name}" and '
                        f"{int(others)} other member(s) are still active. "
                        "Transfer ownership to another member, or delete the "
                        "organization first."
                    ),
                }
            )
            continue
        # Nobody else is left in it: the org goes with the account. Its own
        # deletion blockers (active projects, unresolvable warehouse) are the
        # account's blockers too -- announcing them here is what stops the
        # erasure from failing halfway through in production.
        org_facts = _org_deletion_facts(conn, org_id, name=name, slug=slug)
        for blocker in org_facts["blockers"]:
            blockers.append(
                {
                    "kind": blocker["kind"],
                    "detail": f'Organization "{name}": {blocker["detail"]}',
                }
            )
        orgs_to_erase.append({"org_id": org_id, "name": name, "slug": slug})

    return {
        "identity": identity,
        "email": profile.get("email"),
        "memberships": memberships,
        "sole_owner_of": sole_owner_of,
        "blockers": blockers,
        # Additive, and the honest half of the promise: these organizations are
        # erased WITH the account (warehouse datasets included).
        "organizations_erased_with_account": orgs_to_erase,
    }

def _would_orphan_last_owner(
    cur, org_id: str, target_identity: str, *, new_role: str | None, new_status: str | None
) -> bool:
    """Story 21.5 security follow-up: True if removing/downgrading/suspending
    *target_identity* would leave the org with ZERO active owners.

    An org whose active managers reach zero has zero owners AND admins; if it also
    has no other active members it silently reopens (default-open-until-enrolled),
    and even short of full emptiness it becomes unmanageable. member-management must
    therefore refuse the operation that drops the LAST active manager. This widens
    the earlier owner-only guard: an org whose sole active manager is an ADMIN can no
    longer be emptied via remove/suspend/demote of that admin (FIX 1b).

    RACE (FIX 1a): the active-manager set is locked FOR UPDATE on THIS cursor/txn
    BEFORE it is counted, so two concurrent mutations that each drop a different
    manager serialize instead of both observing the other as still active.
    `
ew_role``/`
ew_status`` are the POST-change values (None for a removal).
    """
    # Lock the org's active-manager set on this transaction so concurrent
    # remove/suspend/demote operations serialize before we count. The lock must be
    # taken BEFORE reading membership so the decision is made on a stable snapshot.
    # MANAGERS, not owners. The docstring above has promised FIX 1b since it was
    # written -- "an org whose sole active manager is an ADMIN can no longer be
    # emptied" -- and this query asked for `role = 'owner'` alone, so the widening
    # it describes never existed. An organization whose only active manager was an
    # admin could be removed, demoted or suspended down to zero managers and became
    # unmanageable; the three tests that assert otherwise were right and had been
    # read as fixture noise. Measured 2026-08-04 on a disposable cluster: they
    # returned 200 where they require 409.
    #
    # A document that describes a state the code does not have is worse than none,
    # because it is believed (CLAUDE.md §3). The code is moved to the docstring
    # rather than the docstring to the code: the wider guard is the one the
    # security follow-up asked for.
    cur.execute(
        "SELECT identity, role FROM app.org_members "
        "WHERE org_id = %s AND status = 'active' AND role IN ('owner', 'admin') "
        "FOR UPDATE",
        (org_id,),
    )
    active_managers = cur.fetchall()

    cur.execute(
        "SELECT role, status FROM app.org_members WHERE org_id = %s AND identity = %s",
        (org_id, target_identity),
    )
    row = cur.fetchone()
    if row is None:
        return False  # not a member -> caller handles the 404
    cur_role, cur_status = row
    was_active_manager = cur_role in ("owner", "admin") and cur_status == "active"
    if not was_active_manager:
        return False  # touching a non-manager never affects the manager floor
    # Post-change: is the target STILL an active manager?
    if new_role is None and new_status is None:
        still_active_manager = False  # removal
    else:
        post_role = new_role if new_role is not None else cur_role
        post_status = new_status if new_status is not None else cur_status
        still_active_manager = post_role in ("owner", "admin") and post_status == "active"
    if still_active_manager:
        return False  # still an active manager -> floor preserved
    # Are there OTHER active managers (from the locked set)?
    other_active_managers = [ident for ident, _role in active_managers if ident != target_identity]
    return len(other_active_managers) == 0

def _would_demote_last_owner(
    cur,
    org_id: str,
    target_identity: str,
    *,
    new_role: str | None,
    new_status: str | None,
) -> bool:
    """True if the change would leave the org with ZERO active owners (AI-348).

    The manager floor above is not enough: demoting the last OWNER while an
    admin remains keeps the org managed and still opens a dead end, because the
    hierarchy rule (FIX 3) forbids that admin from ever assigning `owner` again.
    Measured by `org_membership_walk.py` on 2026-09-01: a 200 that no 200 can
    undo. Ratified in organization-settings.md (amendment 2026-09-02): the
    demotion/suspension/removal of the last active owner is refused, and the
    refusal names the transfer gesture -- promote another active member to
    owner first (an act only an owner may perform), then the change is
    ordinary. Same FOR UPDATE lock discipline as `_would_orphan_last_owner`
    (FIX 1a), on the same cursor and transaction.
    """
    cur.execute(
        "SELECT identity FROM app.org_members "
        "WHERE org_id = %s AND status = 'active' AND role = 'owner' "
        "FOR UPDATE",
        (org_id,),
    )
    active_owners = [row[0] for row in cur.fetchall()]

    cur.execute(
        "SELECT role, status FROM app.org_members WHERE org_id = %s AND identity = %s",
        (org_id, target_identity),
    )
    row = cur.fetchone()
    if row is None:
        return False  # not a member -> caller handles the 404
    cur_role, cur_status = row
    if not (cur_role == "owner" and cur_status == "active"):
        return False  # touching a non-owner never affects the owner floor
    if new_role is None and new_status is None:
        still_active_owner = False  # removal
    else:
        post_role = new_role if new_role is not None else cur_role
        post_status = new_status if new_status is not None else cur_status
        still_active_owner = post_role == "owner" and post_status == "active"
    if still_active_owner:
        return False
    return len([o for o in active_owners if o != target_identity]) == 0


def _transfer_org_ownership(cur, org_id: str, current_owner: str, next_owner: str) -> bool:
    """Atomically promote one active member and demote the current owner."""
    if current_owner == next_owner:
        return False
    cur.execute(
        "SELECT identity, role FROM app.org_members "
        "WHERE org_id = %s AND status = 'active' AND role = 'owner' FOR UPDATE",
        (org_id,),
    )
    owners = {identity for identity, _role in cur.fetchall()}
    if current_owner not in owners:
        return False
    cur.execute(
        "SELECT status FROM app.org_members WHERE org_id = %s AND identity = %s FOR UPDATE",
        (org_id, next_owner),
    )
    target = cur.fetchone()
    if target is None or target[0] != "active":
        return False
    cur.execute(
        "UPDATE app.org_members SET role = 'owner' WHERE org_id = %s AND identity = %s",
        (org_id, next_owner),
    )
    cur.execute(
        "UPDATE app.org_members SET role = 'admin' WHERE org_id = %s AND identity = %s",
        (org_id, current_owner),
    )
    return True

def _count_active_memberships(keys: set[str]) -> int | None:
    """Active memberships held by any of *keys*. ``None`` means UNKNOWN.

    None is not zero, and the caller must not treat it as such: an unverifiable
    count fails closed. Matching is case-insensitive because the two writers of
    ``app.org_members.identity`` disagree on case as well as on which value they
    store (token subject vs verified email).
    """
    candidates = [k.strip().lower() for k in keys if k and k.strip()]
    if not candidates:
        return 0
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.org_members "
                    "WHERE status = 'active' AND LOWER(identity) = ANY(%s)",
                    (candidates,),
                )
                row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: membership_count_failed: %s", exc)
        return None
