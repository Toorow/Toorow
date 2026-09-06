"""Seed the platform rows an evaluation run reads -- into a DISPOSABLE database only.

WHY THIS FILE EXISTS (AI-305). The corpus's business-path dimension scores
`meta.business_context_paths`, which `get_report` resolves from
`app.mdm_business_links`. Two things have to exist in the platform database for
that dimension to measure anything:

1. a MEMBERSHIP for the declared evaluation identity (`core.evaluation_identity`).
   The ratified decision admits that identity past the
   `production_identity_required` door and NOTHING else -- it still has to be a
   member, with a capability, exactly like a person. That is why this seeder
   grants `viewer` + an explicit `view` grant rather than `owner`: an owner needs
   no grant, so an owner row would hide whether the capability floor is still
   being read;
2. a GOVERNED LINK from a business domain to the report the corpus question
   traverses.

WHAT IT REFUSES, AND WHY BOTH REFUSALS (AD-17). The evals tree is TEST CODE, and
this is the one file in it that WRITES to the platform. It refuses unless

* the database name ends in `_test` -- the same rule `scripts/disposable_postgres.py`
  encodes as its trap 3, and the reason a mistyped DSN cannot reach Supabase;
* the process declares itself an evaluation environment -- the same declaration
  that admits the identity at all. A seeder that ran anywhere the identity is
  refused would write governed rows nobody can read.

Either refusal alone would leave a hole: a `_test` database in a production
process, or an evaluation process pointed at a production DSN.

Usage (from the repo root, with the eval loop's disposable Postgres up)::

    TOOROW_ENVIRONMENT=evaluation \\
    PLATFORM_DB_URL=postgresql://connector:...@127.0.0.1:55490/toorow_test \\
      python server/tests/evals/seed_eval_platform.py

It is idempotent: a second run re-reads what the first wrote and changes nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from core.evaluation_identity import (  # noqa: E402
    EVALUATION_ENVIRONMENT,
    EVALUATION_IDENTITY,
    declared_environment,
    evaluation_environment_declared,
)

#: The organization migration 035 gives the `default` project. Named here rather
#: than discovered, so a seeder pointed at the wrong database fails loudly.
EVAL_ORG_ID = "org_default"
EVAL_PROJECT_ID = "default"

#: The governed business domain the corpus names. Its id is DECLARED, not minted:
#: `corpus.yaml` is written offline, long before this seeder runs, and a route it
#: cannot name is a route it cannot expect. Since 2026-08-25 it is written as a
#: fixture row rather than through `create_domain` -- see
#: :func:`_seed_business_domain_fixture`.
EVAL_BUSINESS_DOMAIN_ID = "bdm_EVALUATION_ACQUISITION"
EVAL_BUSINESS_DOMAIN_SLUG = "evaluation-acquisition"
EVAL_BUSINESS_DOMAIN_NAME = "Acquisition (evaluation)"

#: The report the corpus question traverses -- a report definition SHIPPED by the
#: google-analytics module, so no row has to be invented for the report itself.
EVAL_REPORT_TARGET_TYPE = "report_view"
EVAL_REPORT_TARGET_ID = "google-analytics/overview_daily"
EVAL_LINK_RELATION_TYPE = "explains"

_MEMBER_ROW_ID = "omem_EVALUATION"
_GRANT_ROW_ID = "rgrant_EVALUATION"


class EvalSeedRefused(RuntimeError):
    """The target database or the declared environment is not an evaluation one."""


def _database_name(dsn: str) -> str:
    tail = dsn.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0]


def assert_disposable_target(dsn: str) -> None:
    """Refuse anything that is not a declared evaluation environment on a `_test` database."""
    if not evaluation_environment_declared():
        raise EvalSeedRefused(
            "this seeder writes governed rows and refuses to run outside an evaluation "
            f"environment (declared: {declared_environment()!r}). Set "
            f"TOOROW_ENVIRONMENT={EVALUATION_ENVIRONMENT} on the eval loop's process."
        )
    name = _database_name(dsn)
    if not name.endswith("_test"):
        raise EvalSeedRefused(
            f"refusing to seed database {name!r}: an evaluation database name must end "
            "in '_test'. Start one with `python scripts/disposable_postgres.py up`."
        )


def _seed_membership(conn) -> dict[str, str]:
    """Give the evaluation identity a viewer membership and an explicit view grant."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at)
            VALUES (%s, %s, %s, 'viewer', 'active', NOW())
            ON CONFLICT (org_id, identity) DO NOTHING
            """,
            (_MEMBER_ROW_ID, EVAL_ORG_ID, EVALUATION_IDENTITY),
        )
        cur.execute(
            """
            INSERT INTO app.resource_grants
                (id, org_id, identity, scope_type, scope_id, capability, granted_by)
            VALUES (%s, %s, %s, 'project', %s, 'view', %s)
            ON CONFLICT (org_id, identity, scope_type, scope_id) DO NOTHING
            """,
            (
                _GRANT_ROW_ID,
                EVAL_ORG_ID,
                EVALUATION_IDENTITY,
                EVAL_PROJECT_ID,
                EVALUATION_IDENTITY,
            ),
        )
        cur.execute(
            "SELECT role, status FROM app.org_members WHERE org_id = %s AND identity = %s",
            (EVAL_ORG_ID, EVALUATION_IDENTITY),
        )
        row = cur.fetchone()
    return {"role": str(row[0]), "status": str(row[1])} if row else {}


def _seed_business_domain_fixture(conn) -> None:
    """Write the corpus's declared Business Domain as a FIXTURE ROW. Idempotent.

    WHY THIS IS NOT `business_taxonomy.create_domain` ANY MORE. That door refuses
    since 2026-08-25 -- the acceptance schedule Jean ratified: an organization
    converges into Master Data and writes there afterwards. The seam this seeder
    used (`create_domain(..., domain_id=...)`) went with it, and it could not be
    replaced by the gesture that works: `create_link` resolves its taxonomy
    source in the taxonomy store, so a Master Data node alone would leave the
    corpus with no link to traverse. Converging AFTER this row exists changes
    nothing for the corpus either -- the convergence preserves the id.

    WHAT KEEPS THIS HONEST. It is a fixture, and it is written like the two
    fixtures above it: explicit id, `ON CONFLICT DO NOTHING`, inside a function
    that `assert_disposable_target` has already refused to reach unless the
    process declares itself an evaluation environment AND the database name ends
    in `_test`. The version row is written with it -- migration 130 seeds the six
    supplied domains exactly this way, `change_kind = 'seeded'` -- because a
    domain with no version row is a shape no reader of this store ever sees.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_business_domains
                (id, org_id, slug, name, description, owner, status, created_by)
            VALUES (%s, %s, %s, %s, %s, NULL, 'active', %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                EVAL_BUSINESS_DOMAIN_ID,
                EVAL_ORG_ID,
                EVAL_BUSINESS_DOMAIN_SLUG,
                EVAL_BUSINESS_DOMAIN_NAME,
                "Seeded by the evaluation loop; never present in production.",
                EVALUATION_IDENTITY,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.mdm_business_domain_versions
                (domain_id, version_number, org_id, slug, name, description, owner,
                 status, created_by, created_at, updated_at, archived_at,
                 change_kind, changed_by)
            SELECT id, 1, org_id, slug, name, description, owner, status, created_by,
                   created_at, updated_at, archived_at, 'seeded', %s
              FROM app.mdm_business_domains
             WHERE id = %s
               AND NOT EXISTS (
                   SELECT 1 FROM app.mdm_business_domain_versions WHERE domain_id = %s
               )
            """,
            (EVALUATION_IDENTITY, EVAL_BUSINESS_DOMAIN_ID, EVAL_BUSINESS_DOMAIN_ID),
        )


def _seed_governed_link(conn, *, loaded_modules) -> str:
    """Seed the business domain fixture, then the governed link through its writer."""
    from core import business_taxonomy  # noqa: PLC0415

    _seed_business_domain_fixture(conn)

    existing = [
        link
        for link in business_taxonomy.list_links(
            conn, org_id=EVAL_ORG_ID, project_id=EVAL_PROJECT_ID
        )
        if link["target_type"] == EVAL_REPORT_TARGET_TYPE
        and link["target_id"] == EVAL_REPORT_TARGET_ID
    ]
    if existing:
        return str(existing[0]["id"])
    link = business_taxonomy.create_link(
        conn,
        org_id=EVAL_ORG_ID,
        project_id=EVAL_PROJECT_ID,
        taxonomy_type="business_domain",
        taxonomy_id=EVAL_BUSINESS_DOMAIN_ID,
        target_type=EVAL_REPORT_TARGET_TYPE,
        target_id=EVAL_REPORT_TARGET_ID,
        relation_type=EVAL_LINK_RELATION_TYPE,
        actor=EVALUATION_IDENTITY,
        reason="evaluation corpus business-path dimension (AI-305)",
        loaded_modules=loaded_modules,
    )
    return str(link["id"])


def seed(conn, *, loaded_modules=None) -> dict[str, object]:
    """Seed membership + governed link on an open connection. Idempotent."""
    if loaded_modules is None:
        from core.main import _loaded_modules  # noqa: PLC0415

        loaded_modules = _loaded_modules
    membership = _seed_membership(conn)
    link_id = _seed_governed_link(conn, loaded_modules=loaded_modules)
    conn.commit()
    return {
        "identity": EVALUATION_IDENTITY,
        "org_id": EVAL_ORG_ID,
        "membership": membership,
        "domain_id": EVAL_BUSINESS_DOMAIN_ID,
        "link_id": link_id,
        "target": f"{EVAL_REPORT_TARGET_TYPE}:{EVAL_REPORT_TARGET_ID}",
    }


def main(argv: list[str] | None = None) -> int:
    del argv
    os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
    os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
    os.environ.setdefault("SCHEDULER_ENABLED", "false")

    from core import db as core_db  # noqa: PLC0415

    dsn = core_db._db_url()
    try:
        assert_disposable_target(dsn)
    except EvalSeedRefused as exc:
        print(f"REFUSED: {exc}")
        return 2
    with core_db.get_connection() as conn:
        result = seed(conn)
    for key, value in result.items():
        print(f"  {key:12s} {value}")
    print("eval platform seed OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
