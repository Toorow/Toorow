"""Apply the validated SQL migration catalog with an atomic database ledger."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from check_migration_catalog import (
    MigrationCatalogError,
    canonical_checksum,
    validate_catalog,
)

_BOUNDARY = re.compile(r"^\s*(BEGIN|COMMIT);\s*(?:--.*)?$", re.IGNORECASE)
_LOCK_NAME = "toorow:application-migrations"
_MIGRATION_251_CHECKSUM = "658d2a90cfbf29b98fca8859863b6da6f35e1aea5b1900b8a0aea089a084f7dc"
_LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS toorow_meta;
CREATE TABLE IF NOT EXISTS toorow_meta.schema_migrations (
    identifier  INTEGER PRIMARY KEY,
    filename    TEXT NOT NULL,
    checksum    TEXT NOT NULL CHECK (length(checksum) = 64),
    status      TEXT NOT NULL CHECK (status IN ('applied', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 1 CHECK (attempts > 0),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at  TIMESTAMPTZ,
    failed_at   TIMESTAMPTZ,
    sqlstate    TEXT,
    error       TEXT
);
"""


class MigrationApplyError(RuntimeError):
    """A migration could not be validated, applied, or recorded safely."""


@dataclass(frozen=True)
class Migration:
    identifier: int
    path: Path
    checksum: str
    body: str

    @property
    def filename(self) -> str:
        return self.path.name


def _compatibility_prelude(migration: Migration) -> str:
    """Repair the sole known fresh-install parser dependency without checksum drift.

    Migration 251 was already applied with this checksum before its fresh-install
    path exposed PostgreSQL's sibling ALTER COLUMN name resolution. A 252/253
    migration cannot repair a predecessor that never commits, so the official
    runner performs the idempotent ADD in the same transaction immediately before
    the byte-identical 251 body. Existing ledgers skip 251 and therefore skip this.
    """
    if (
        migration.identifier == 251
        and migration.filename == "251_feedback_review_exact_cohorts.sql"
        and migration.checksum == _MIGRATION_251_CHECKSUM
    ):
        return (
            "ALTER TABLE app.render_share_feedback ADD COLUMN IF NOT EXISTS observed_surface TEXT;"
        )
    return ""


def _migration_body(path: Path) -> str:
    """Remove one optional outer transaction pair for runner-owned atomicity."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    boundaries = [
        (index, match.group(1).upper())
        for index, line in enumerate(lines)
        if (match := _BOUNDARY.fullmatch(line.rstrip("\r\n"))) is not None
    ]
    if not boundaries:
        return text
    code_lines = [
        index
        for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("--")
    ]
    valid_pair = (
        len(boundaries) == 2
        and boundaries[0][1] == "BEGIN"
        and boundaries[1][1] == "COMMIT"
        and boundaries[0][0] == code_lines[0]
        and boundaries[1][0] == code_lines[-1]
    )
    if not valid_pair:
        rendered = ", ".join(kind for _, kind in boundaries) or "none"
        raise MigrationCatalogError(
            f"invalid transaction boundaries in {path.name}: expected outer BEGIN, COMMIT; "
            f"got {rendered}"
        )
    excluded = {boundaries[0][0], boundaries[1][0]}
    return "".join(line for index, line in enumerate(lines) if index not in excluded)


def load_migrations(directory: Path, *, verify_manifest: bool = False) -> list[Migration]:
    migrations: list[Migration] = []
    for path in validate_catalog(directory, verify_manifest=verify_manifest):
        migrations.append(
            Migration(
                identifier=int(path.name[:3]),
                path=path,
                checksum=canonical_checksum(path),
                body=_migration_body(path),
            )
        )
    return migrations


def _ledger_exists(conn: Any) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('toorow_meta.schema_migrations') IS NOT NULL")
        return bool(cursor.fetchone()[0])


def _prepare_ledger(conn: Any) -> None:
    if _ledger_exists(conn):
        return
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'app')"
        )
        if cursor.fetchone()[0]:
            raise MigrationApplyError(
                "existing app schema has no migration ledger; controlled adoption required"
            )
        cursor.execute(_LEDGER_DDL)
        cursor.execute("REVOKE ALL ON SCHEMA toorow_meta FROM PUBLIC")
        cursor.execute("REVOKE ALL ON TABLE toorow_meta.schema_migrations FROM PUBLIC")


def _ledger_rows(conn: Any) -> dict[int, tuple[str, str, str]]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT identifier, filename, checksum, status "
            "FROM toorow_meta.schema_migrations ORDER BY identifier"
        )
        return {
            int(identifier): (str(filename), str(checksum), str(status))
            for identifier, filename, checksum, status in cursor.fetchall()
        }


def _validate_ledger(
    migrations: list[Migration],
    rows: dict[int, tuple[str, str, str]],
    *,
    fill_gaps: frozenset[int] = frozenset(),
) -> None:
    catalog = {migration.identifier: migration for migration in migrations}
    for identifier, (filename, checksum, status) in rows.items():
        migration = catalog.get(identifier)
        if migration is None:
            raise MigrationApplyError(
                f"ledger contains unknown migration {identifier:03d}: {filename}"
            )
        if migration.filename != filename:
            raise MigrationApplyError(
                f"migration {identifier:03d} filename drift: ledger has {filename}, "
                f"catalog has {migration.filename}"
            )
        if status == "applied" and migration.checksum != checksum:
            raise MigrationApplyError(
                f"migration {identifier:03d} checksum drift: applied migration changed"
            )

    if rows:
        highest = max(rows)
        missing = [identifier for identifier in range(1, highest + 1) if identifier not in rows]
        unauthorized = [identifier for identifier in missing if identifier not in fill_gaps]
        if unauthorized:
            rendered = ", ".join(f"{identifier:03d}" for identifier in unauthorized)
            flags = " --fill-gap ".join(f"{identifier:d}" for identifier in unauthorized)
            raise MigrationApplyError(
                f"migration ledger is not continuous; missing: {rendered}. "
                "Nothing can be applied until the hole is closed. To close it, name it: "
                f"--fill-gap {flags}"
            )
        failed = [identifier for identifier, row in rows.items() if row[2] == "failed"]
        if failed and (len(failed) > 1 or failed[0] != highest):
            raise MigrationApplyError(
                "migration ledger has applied entries after a failed migration"
            )


def _verify_schema_contract(conn: Any) -> None:
    """Check the critical organization, invitation, ENTRY, and hourly seams."""
    critical_tables = (
        "app.organizations",
        "app.org_members",
        "app.projects",
        "app.operations",
        "app.invitations",
        "app.invitation_exchange_sessions",
        "app.persons",
        "app.person_identities",
        "app.instance_bootstrap_capabilities",
        "app.instance_bootstrap_exchange_sessions",
        "app.instance_members",
        "app.instance_claims",
        "app.hosted_entry_scope_consumptions",
    )
    with conn.cursor() as cursor:
        for table in critical_tables:
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
            if not cursor.fetchone()[0]:
                raise MigrationApplyError(f"schema postcondition missing table: {table}")

        for table, column in (
            ("invitations", "org_id"),
            ("operations", "effective_org_id"),
        ):
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = %s AND column_name = %s",
                (table, column),
            )
            row = cursor.fetchone()
            if row is None or row[0] != "YES":
                raise MigrationApplyError(
                    f"schema postcondition requires nullable app.{table}.{column}"
                )

        cursor.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'app' AND t.relname = 'datastreams' "
            "AND c.conname = 'datastreams_schedule_mode_check'"
        )
        row = cursor.fetchone()
        values = set(re.findall(r"'([^']+)'", row[0] if row else ""))
        if values != {"nightly", "manual", "hourly", "weekly"}:
            raise MigrationApplyError(
                "schema postcondition invalid datastreams_schedule_mode_check"
            )

        cursor.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'app' AND t.relname = 'invitations' "
            "AND c.conname = 'ck_invitation_grants_require_org'"
            ")"
        )
        if not cursor.fetchone()[0]:
            raise MigrationApplyError(
                "schema postcondition missing constraint: ck_invitation_grants_require_org"
            )
        cursor.execute("SELECT to_regclass('app.operations_platform_idempotency') IS NOT NULL")
        if not cursor.fetchone()[0]:
            raise MigrationApplyError(
                "schema postcondition missing index: operations_platform_idempotency"
            )


def _why(exc: Exception) -> str:
    """The class AND the sentence. One without the other names no repair.

    Measured 2026-08-17: migration 273 declares RLS policies on 67 tables, so it
    needs the schema OWNER. Run as the deployed application role it failed, and
    the only thing anyone was told -- on screen and in the ledger -- was
    `migration 273 ... failed` and the word `InsufficientPrivilege`. Neither says
    WHICH object, so the runner had to be bypassed and the file applied by hand
    to learn that the answer was "must be owner of table ...". A tool that
    refuses without naming what it lacks costs exactly that detour.
    """
    detail = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _record_failure(conn: Any, migration: Migration, exc: Exception) -> None:
    sqlstate = getattr(exc, "sqlstate", None)
    message = _why(exc)[:2000]
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO toorow_meta.schema_migrations
                    (identifier, filename, checksum, status, failed_at, sqlstate, error)
                VALUES (%s, %s, %s, 'failed', now(), %s, %s)
                ON CONFLICT (identifier) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    checksum = EXCLUDED.checksum,
                    status = 'failed',
                    attempts = toorow_meta.schema_migrations.attempts + 1,
                    failed_at = now(),
                    applied_at = NULL,
                    sqlstate = EXCLUDED.sqlstate,
                    error = EXCLUDED.error
                """,
                (
                    migration.identifier,
                    migration.filename,
                    migration.checksum,
                    sqlstate,
                    message,
                ),
            )


def _validate_gap_requests(
    identifiers: set[int],
    rows: dict[int, tuple[str, str, str]],
    fill_gaps: frozenset[int],
) -> None:
    """Refuse a --fill-gap that does not name an actual hole in the ledger.

    A hole is an identifier the ledger has NO row for at all, below its head. A
    recorded failure is a retry (the runner already reapplies it), and anything
    above the head is ordinary pending work. Letting the flag cover those cases
    would turn a narrow repair into a way to wave the continuity guard through.
    """
    head = max(rows) if rows else 0
    for identifier in sorted(fill_gaps):
        if identifier not in identifiers:
            raise MigrationApplyError(f"--fill-gap {identifier:03d} does not exist in the catalog")
        if identifier in rows:
            raise MigrationApplyError(
                f"--fill-gap {identifier:03d} is not a gap: the ledger records it as "
                f"{rows[identifier][2]}"
            )
        if identifier > head:
            raise MigrationApplyError(
                f"--fill-gap {identifier:03d} is ahead of ledger head {head:03d}; "
                "that is ordinary pending work, applied without a flag"
            )


def apply_migrations(
    conn: Any,
    migrations: list[Migration],
    *,
    target: int | None = None,
    fill_gaps: frozenset[int] = frozenset(),
) -> tuple[list[int], list[int]]:
    """Apply pending migrations through target; return (applied, skipped).

    ``fill_gaps`` names identifiers that are missing BELOW the ledger head. The
    continuity guard fires before anything is applied, so without this the runner
    can report a hole and has no path to close it -- including the migration that
    would close it. Each named gap is still applied by the runner itself, its DDL
    and its ledger row in ONE transaction: naming a gap authorizes the repair, it
    never hand-writes a ledger line.
    """
    identifiers = {migration.identifier for migration in migrations}
    if target is not None and target not in identifiers:
        raise MigrationApplyError(f"migration target does not exist: {target:03d}")
    _prepare_ledger(conn)
    rows = _ledger_rows(conn)
    _validate_gap_requests(identifiers, rows, fill_gaps)
    _validate_ledger(migrations, rows, fill_gaps=fill_gaps)
    if target is not None and rows and target < max(rows):
        raise MigrationApplyError(
            f"migration target {target:03d} precedes ledger head {max(rows):03d}"
        )
    applied: list[int] = []
    skipped: list[int] = []

    for migration in migrations:
        if target is not None and migration.identifier > target:
            break
        row = rows.get(migration.identifier)
        if row is not None and row[2] == "applied":
            skipped.append(migration.identifier)
            continue
        try:
            with conn.transaction():
                with conn.cursor() as cursor:
                    prelude = _compatibility_prelude(migration)
                    if prelude:
                        cursor.execute(prelude)
                    cursor.execute(migration.body)
                    cursor.execute(
                        """
                        INSERT INTO toorow_meta.schema_migrations
                            (identifier, filename, checksum, status, applied_at)
                        VALUES (%s, %s, %s, 'applied', now())
                        ON CONFLICT (identifier) DO UPDATE SET
                            filename = EXCLUDED.filename,
                            checksum = EXCLUDED.checksum,
                            status = 'applied',
                            attempts = toorow_meta.schema_migrations.attempts + 1,
                            applied_at = now(),
                            failed_at = NULL,
                            sqlstate = NULL,
                            error = NULL
                        """,
                        (migration.identifier, migration.filename, migration.checksum),
                    )
        except Exception as exc:
            try:
                _record_failure(conn, migration, exc)
            except Exception as record_exc:
                raise MigrationApplyError(
                    f"migration {migration.identifier:03d} {migration.filename} failed; "
                    "failure status could not be recorded"
                ) from record_exc
            raise MigrationApplyError(
                f"migration {migration.identifier:03d} {migration.filename} failed -- "
                f"{_why(exc)}"
            ) from exc
        applied.append(migration.identifier)
        rows[migration.identifier] = (
            migration.filename,
            migration.checksum,
            "applied",
        )
    if target is None or target == migrations[-1].identifier:
        _verify_schema_contract(conn)
    return applied, skipped


def verify_complete(conn: Any, migrations: list[Migration]) -> list[int]:
    """Fail unless every catalog migration is recorded as applied."""
    if not _ledger_exists(conn):
        raise MigrationApplyError("migration ledger does not exist")
    rows = _ledger_rows(conn)
    _validate_ledger(migrations, rows)
    pending = [
        migration.identifier
        for migration in migrations
        if rows.get(migration.identifier, ("", "", "pending"))[2] != "applied"
    ]
    if pending:
        rendered = ", ".join(f"{identifier:03d}" for identifier in pending)
        raise MigrationApplyError(f"pending migrations: {rendered}")
    _verify_schema_contract(conn)
    return [migration.identifier for migration in migrations]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN; defaults to platform env")
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path("infra/nango/migrations"),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--target", type=int, default=None)
    action.add_argument("--verify-complete", action="store_true")
    parser.add_argument(
        "--fill-gap",
        type=_positive_int,
        action="append",
        default=None,
        metavar="IDENTIFIER",
        dest="fill_gap",
        help=(
            "authorize applying a migration that is MISSING below the ledger head. "
            "Repeatable. The runner still applies it and writes its ledger row in one "
            "transaction; this only says which hole you meant to close."
        ),
    )
    parser.add_argument("--connect-timeout-seconds", type=_positive_int, default=10)
    parser.add_argument("--lock-timeout-seconds", type=_positive_int, default=30)
    parser.add_argument("--statement-timeout-seconds", type=_positive_int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = args.dsn or os.getenv("PLATFORM_DB_URL") or os.getenv("PLATFORM_DATABASE_URL")
    if not dsn:
        print("migration runner requires --dsn or PLATFORM_DB_URL", file=sys.stderr)
        return 2
    fill_gaps = frozenset(args.fill_gap or ())
    if fill_gaps and args.verify_complete:
        print(
            "--fill-gap repairs the ledger; --verify-complete only reads it",
            file=sys.stderr,
        )
        return 2
    try:
        migrations = load_migrations(args.migrations_dir, verify_manifest=True)
        import psycopg

        with psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=args.connect_timeout_seconds,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, false)",
                    (f"{args.lock_timeout_seconds}s",),
                )
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (f"{args.statement_timeout_seconds}s",),
                )
                cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (_LOCK_NAME,))
                if not cursor.fetchone()[0]:
                    raise MigrationApplyError("another migration runner holds the advisory lock")
            try:
                if args.verify_complete:
                    verified = verify_complete(conn, migrations)
                    print(f"migration ledger complete: {len(verified)} applied")
                    return 0
                applied, skipped = apply_migrations(
                    conn,
                    migrations,
                    target=args.target,
                    fill_gaps=fill_gaps,
                )
            finally:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))
    except (MigrationCatalogError, MigrationApplyError) as exc:
        print(f"migration runner failed: {exc}", file=sys.stderr)
        return 1
    filled = sorted(fill_gaps & set(applied))
    if filled:
        rendered = ", ".join(f"{identifier:03d}" for identifier in filled)
        print(f"migration runner CLOSED A LEDGER HOLE: {rendered} applied out of order")
    print(f"migration runner OK: {len(applied)} applied, {len(skipped)} already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
