"""Stand up a disposable PostgreSQL so pg-gated suites can actually run.

Why this exists (AI-82, review-epic-48.md H-2 / review-epic-49.md C-5)
---------------------------------------------------------------------
175 test files under `server/tests/` skip themselves when `TEST_POSTGRES_DSN` is
unset. Three completeness-ledger entries cite numbers produced with such a
database, and two of them overstated what a reader gets without one::

    ledger tax-fees [1]  "79 passed"   -> 57 passed, 22 skipped
    ledger tax-fees [2]  "53 passed"   -> 53 skipped

CLAUDE.md is explicit: no figure without the command that produces it. A command
that returns the opposite for the reader does not produce the figure. The repair
is not to delete the claims -- they are true with a database -- but to make the
database one command away, and to say so in the entry.

Usage
-----
::

    python scripts/disposable_postgres.py up      # create, migrate, print the DSN
    eval "$(python scripts/disposable_postgres.py env)"   # export it into a shell
    python scripts/disposable_postgres.py down    # stop and delete everything

Four traps this encodes, each of which has cost a session a green run that
proved nothing:

1. **Never connect as a superuser.** RLS is not enforced for superusers or roles
   with BYPASSRLS, so every isolation assertion passes vacuously. The suites run
   as an ordinary `connector` role.
2. **The role must exist before migration 002**, which grants to it. Creating it
   afterwards leaves the grants unapplied and the failure surfaces much later.
3. **The database name must end in `_test`.** This refuses to run against
   anything else, so a mistyped DSN cannot reach Supabase -- the accident this
   repository has already paid for (memory `tests-must-not-write-to-prod`).
4. **Keep the data directory path short.** Windows named pipes and socket paths
   truncate, and initdb fails with an error that names neither.
5. **Never capture `pg_ctl start`'s output.** On Windows the postmaster inherits
   the pipe, EOF never arrives, and `subprocess.run(capture_output=True)` blocks
   forever on a child that has already exited -- a HEALTHY server, and a script
   that never reaches the next line. Cost 25 minutes on 2026-08-04 before being
   reproduced in isolation. See the comment at the call site; the numbered TRAP 5
   below is a different, older thing and keeps its number for the sessions that
   cite it.

TRAP 5, MESURE LE 2026-08-03, **FERME LE 2026-08-04 PAR LA MIGRATION 207**
(AI-150). Gardee ici en entier parce que deux sessions y ont perdu une demi-
journee chacune et que le message d'erreur ne nomme jamais la cause. Cette recette
faisait de `connector` le PROPRIETAIRE de la base, alors qu'en production le
proprietaire est `postgres`. La divergence est invisible jusqu'a ce qu'une
migration revoque au proprietaire :

    183: REVOKE ALL ON TABLE app.datastream_inbound_credential_rate_events
             FROM PUBLIC, connector;

Revoquer au proprietaire materialise un ACL VIDE (`relacl = '{}'`), ce qui retire
au proprietaire ses droits implicites -- y compris TRIGGER. La migration 199
echoue alors sur son `CREATE TRIGGER`, avec « droit refuse pour la table », un
message qui ne nomme ni le proprietaire ni l'ACL. En production, ou `connector`
n'est pas proprietaire, le meme SQL passe.

Deblocage immediat, en attendant la separation des roles :

    psql -U postgres -d toorow_test -c "GRANT ALL
        ON app.datastream_inbound_credential_rate_events TO connector;"

⚠️ CE `GRANT` DISAIT `TRIGGER, REFERENCES` ET CE N'ETAIT PAS ASSEZ (mesure le
2026-08-04, session 48.4). La 199 passe avec ces deux droits, mais la migration
n'est pas le seul chemin : `DELETE FROM app.datastreams` declenche la
verification de cle etrangere de cette table, qui execute
`SELECT 1 FROM ... FOR KEY SHARE`. Une clause de verrouillage exige UPDATE ou
DELETE, pas SELECT -- donc `GRANT SELECT, REFERENCES, TRIGGER` laisse encore
`test_31_fk_cascade_needs_no_rgpd_allowlist_edit` rouge, avec le meme message
qui ne nomme ni le proprietaire ni l'ACL. `GRANT ALL` reproduit exactement ce que
le proprietaire detient implicitement en production, ou le declencheur RI
s'execute avec ses droits. Suivre la ligne precedente coutait une deuxieme
demi-heure sur le meme piege.

CE QUI A ETE FAIT, ET DANS QUEL ORDRE -- la premiere tentative etait fausse et
c'est la mesure qui l'a dit. Creer la base `OWNER postgres` et migrer comme
`postgres` appliquait 206/206 migrations sans erreur, puis laissait **104 des 289
tables illisibles** a `connector` : le schema entier reposait sur la propriete,
aucune migration n'emettant `ALTER DEFAULT PRIVILEGES` et quatre seulement un
`GRANT ... connector`.

La production a alors ete mesuree, ce qui etait le vrai prealable :

    proprietaire de app.projects ............ postgres
    tables app/toorow_meta .................. 287
    dont `connector` pouvait en lire ........ 0
    role dont l'application se sert .......... postgres, rolbypassrls = true

Donc `connector` n'etait pas « le role applicatif avec moins de droits » : il ne
servait a rien du tout, et l'application tourne sous un role qui **saute toutes
les politiques RLS**. La migration 207 donne a `connector` les droits que le
schema n'avait jamais accordes (plus `ALTER DEFAULT PRIVILEGES` pour les tables a
venir), en re-posant verbatim les REVOKE que les migrations avaient declares.
Elle ne change RIEN pour l'application : `connector` est NOLOGIN, aucune session
ne l'endosse.

Verifie apres 207, en local ET en production : 1 seule table reste illisible, et
c'est celle que la 183 verrouille expres (SELECT au niveau colonne uniquement).

CE QUI RESTE, ET QUI N'EST PAS ICI : basculer le DSN de l'application sur
`connector` pour que les politiques RLS cessent d'etre vacantes. C'est un acte de
deploiement, pas un changement de schema -- c'est AI-100.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DB_NAME = "toorow_test"
DB_ROLE = "connector"
DB_PASSWORD = "connector_local_only"
DEFAULT_PORT = 55432

# Short on purpose -- see trap 4 in the module docstring.
STATE_DIR = Path(tempfile.gettempdir()) / "toorow_pg"


def data_dir(port: int = DEFAULT_PORT) -> Path:
    """The cluster directory for *port*. One directory per port, never one for all.

    `--port` was parameterised and this path was not, so a second session running
    `up --port N` was told « cluster already present; run `down` first » -- about
    somebody else's cluster -- and `down` would have DELETED it. The only exits
    were to destroy a neighbour's work or to give up on the script: on 2026-08-03
    a session gave up and hand-rolled a separate cluster at `C:\\pgclaude`
    (port 55499), which is how this defect is known rather than supposed.

    The default port keeps its historical directory EXACTLY, so a cluster that is
    already up right now still answers to its own `down`. Renaming it would strand
    a running cluster with no way to stop it -- the same destruction by another
    door.
    """
    return STATE_DIR / ("data" if port == DEFAULT_PORT else f"data-{port}")


def log_file(port: int = DEFAULT_PORT) -> Path:
    """The postmaster log for *port*. Per-port for a reason MEASURED, not guessed.

    The first attempt at a second cluster fixed `data_dir` and left this path
    shared, and `pg_ctl start` failed with exit 1 and no explanation: on Windows
    the neighbour's LIVE postmaster holds its log open, so the new one cannot
    write it. The error names neither the file nor the lock -- exactly the shape
    of trap 4 above, and the reason it is written down here instead of rediscovered.
    """
    return STATE_DIR / ("server.log" if port == DEFAULT_PORT else f"server-{port}.log")


#: Kept for readers and callers that mean the default cluster.
DATA_DIR = data_dir()

BINARY_HINT = """\
No PostgreSQL binaries were found. This script does not download them: fetching
several hundred megabytes as a side effect of running a test suite is not
something a script should decide on your behalf.

Install either one, then re-run:

  * EDB binaries (no installer, no service, unzip and go):
        https://www.enterprisedb.com/download-postgresql-binaries
    unzip somewhere with a SHORT path, then either put its `bin/` on PATH or
    point this script at it:
        set PG_BIN=C:\\pgsql\\bin        (Windows)
        export PG_BIN=/opt/pgsql/bin    (POSIX)

  * or any local PostgreSQL >= 14 whose `initdb` and `pg_ctl` are on PATH.

Nothing else is required: this script creates its own cluster on port {port},
its own `{role}` role and its own `{db}` database, and `down` removes all of it.
"""


def _bin(name: str) -> str:
    """Resolve a PostgreSQL executable from PG_BIN or PATH."""
    override = os.environ.get("PG_BIN")
    if override:
        candidate = Path(override) / name
        for suffix in ("", ".exe"):
            if candidate.with_suffix(suffix).exists():
                return str(candidate.with_suffix(suffix))
    found = shutil.which(name)
    if found:
        return found
    raise SystemExit(BINARY_HINT.format(port=DEFAULT_PORT, role=DB_ROLE, db=DB_NAME))


def dsn(port: int = DEFAULT_PORT) -> str:
    """What the SUITES connect with: the application role, exactly like production."""
    return f"postgresql://{DB_ROLE}:{DB_PASSWORD}@127.0.0.1:{port}/{DB_NAME}"


def owner_dsn(port: int = DEFAULT_PORT) -> str:
    """What the MIGRATIONS connect with: the owner, which is not the application role.

    Keeping these two apart is the whole of trap 5. `initdb --auth=trust` above is
    why no password appears here.
    """
    return f"postgresql://postgres@127.0.0.1:{port}/{DB_NAME}"


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kwargs)


def _psql(port: int, sql: str, *, database: str = "postgres") -> str:
    """Run SQL as the bootstrap superuser, which exists only to create the role.

    That superuser is `postgres`, and only `postgres`: `initdb` above is called
    with an explicit `-U postgres`, so it is the ONE role the fresh cluster
    contains. Deriving this name from `$USER`/`$USERNAME` instead made the very
    next statement fail with `role "<you>" does not exist` on every machine whose
    login is not literally `postgres` -- which on Windows is every machine. The
    failure left a started server with no role and no database behind it, and
    `up` then refuses to retry because the data directory exists, so the next
    session inherits a cluster that looks alive and answers nothing.
    """
    proc = _run(
        [
            _bin("psql"),
            "-h", "127.0.0.1",
            "-p", str(port),
            "-d", database,
            "-U", "postgres",
            "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ]
    )
    return proc.stdout


def up(port: int) -> int:
    target = data_dir(port)
    if DB_NAME.rsplit("_", 1)[-1] != "test":  # trap 3, checked before anything runs
        raise SystemExit(f"refusing: database name {DB_NAME!r} does not end in _test")

    if target.exists():
        print(f"cluster already present at {target}; run `down` first", file=sys.stderr)
        return 1

    # Resolve every binary before touching the filesystem, so a missing install
    # explains itself instead of leaving a half-made state directory behind.
    initdb, pg_ctl = _bin("initdb"), _bin("pg_ctl")
    _bin("psql")

    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"initdb -> {target}")
    _run([initdb, "-D", str(target), "-U", "postgres", "--auth=trust", "-E", "UTF8"])

    print(f"starting on port {port}")
    # NOT `_run`, and this is trap 6: `capture_output=True` HANGS FOREVER HERE.
    #
    # `pg_ctl start` exits as soon as `-w` confirms the server is up, but on Windows
    # the postmaster it leaves behind INHERITS the pipe handles that `capture_output`
    # created. `subprocess.run` reads that pipe until EOF, and EOF only comes when
    # the last holder closes it -- which is the server, at shutdown. So the call
    # blocks on a child that has already exited, and the script never reaches the
    # `CREATE ROLE` on the next line.
    #
    # Measured 2026-08-04, not deduced: an `up --port 55480` sat for 25 minutes with
    # a healthy server (`pg_isready` -> accepting connections) and no role, and the
    # postmaster log showed the cluster ready 15 minutes before the first client.
    # Reproduced in isolation -- the same `subprocess.run(..., capture_output=True)`
    # never returned and had to be killed at 100s.
    #
    # `-l` already sends everything the SERVER says to the log file, so the only
    # output lost here is pg_ctl's own "waiting for server to start... done", and
    # `check=True` still raises on a real failure.
    subprocess.run(
        [
            pg_ctl, "-D", str(target),
            "-o", f"-p {port} -c listen_addresses=127.0.0.1",
            "-l", str(log_file(port)), "start", "-w",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Trap 1 and 2: an ordinary role, created BEFORE the migrations that grant to
    # it. No SUPERUSER, no BYPASSRLS -- otherwise every RLS assertion is vacuous.
    print(f"creating role {DB_ROLE} (no superuser, no bypassrls) and {DB_NAME}")
    _psql(port, f"CREATE ROLE {DB_ROLE} LOGIN PASSWORD '{DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS")

    # Trap 5, added by Story 50.7. `toorow_share_reader` is the public share
    # path's database role: it holds SELECT/INSERT on four share tables and NO
    # privilege at all on `app.query_results` and friends, so a public handler
    # that asks for Project data is refused by PostgreSQL (42501) rather than by
    # a handler remembering not to ask.
    #
    # It is created HERE, by the bootstrap superuser, and not by migration 162,
    # because `{DB_ROLE}` has neither SUPERUSER nor CREATEROLE -- a `CREATE ROLE`
    # inside the migration could only ever be wrapped in an exception handler,
    # and a handler that swallows `insufficient_privilege` yields a migration
    # reporting success while the enforcement layer silently does not exist.
    # Migration 162 RAISES if this role is missing, so a cluster bootstrapped
    # without these two statements fails loudly at migrate time.
    _psql(port, "CREATE ROLE toorow_share_reader NOLOGIN NOSUPERUSER NOBYPASSRLS")
    _psql(port, f"GRANT toorow_share_reader TO {DB_ROLE}")

    # Trap 5, CLOSED by migration 207. The database is owned by `postgres` and
    # the migrations run as `postgres`, exactly as in production (measured
    # 2026-08-04: `app.projects` is owned by `postgres` there). `{DB_ROLE}` keeps
    # only what the migrations GRANT it.
    #
    # `OWNER {DB_ROLE}` is what this said, and that one word made the local
    # cluster a different database from production in the way that matters: a
    # REVOKE against the OWNER materialises an EMPTY acl, which strips the owner's
    # implicit rights. Migration 199 failed its CREATE TRIGGER locally and passed
    # in production, naming neither the owner nor the acl; two sessions lost half
    # a day each, and the `GRANT ALL` they fell back on hid the divergence.
    _psql(port, f"CREATE DATABASE {DB_NAME} OWNER postgres")

    print("applying migrations as postgres (the owner), not as the application role")
    env = dict(os.environ, PLATFORM_DB_URL=owner_dsn(port), TEST_POSTGRES_DSN=owner_dsn(port))
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "apply_migrations.py")],
        env=env, cwd=str(root), capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("migrations failed; the cluster is left up so you can inspect it")

    print(f"\nready:\n  TEST_POSTGRES_DSN={dsn(port)}")
    print("  run `eval \"$(python scripts/disposable_postgres.py env)\"` to export it")
    return 0


def env(port: int) -> int:
    """Print the exports a shell needs. `down` removes every trace of them.

    TWO DSNs, because there are two roles and conflating them is the whole of
    trap 5. `TEST_POSTGRES_DSN` is the application role, and it is what the suites
    want: it is the only one under which an RLS assertion means anything.

    `TEST_POSTGRES_OWNER_DSN` is the owner, and exactly one file needs it --
    `tests/integration/test_managed_feed_ledger_constraints.py`, whose fixture
    creates and drops objects. It says so itself and skips rather than lie:

        needs an owning role for its DDL fixture; connected as 'connector'.
        Point TEST_POSTGRES_DSN at the schema owner for this file.

    Measured 2026-08-04: 41 of its tests ran under the old recipe only because
    `connector` wrongly OWNED the schema. They are not lost, they are addressed --
    run that one file with the owner DSN.
    """
    print(f"export TEST_POSTGRES_DSN='{dsn(port)}'")
    print(f"export PLATFORM_DB_URL='{dsn(port)}'")
    print(f"export TEST_POSTGRES_OWNER_DSN='{owner_dsn(port)}'")
    print("export HEALTH_POLLER_ENABLED=false QUEUE_WORKER_ENABLED=false SCHEDULER_ENABLED=false")
    return 0


def down(port: int) -> int:
    target = data_dir(port)
    if target.exists():
        # `_bin` RAISES SystemExit when the binaries are not on PATH, and it used
        # to be called here first -- so `down` aborted before touching anything
        # and printed the fifteen-line "install PostgreSQL" wall while a cluster
        # this very script had started was RUNNING. Measured 2026-08-16: exit 1,
        # nothing removed, and a message about an installation that plainly
        # exists. A teardown that cannot say why it did not tear down is how a
        # stale cluster survives a session -- and a stale cluster is how a ledger
        # comes to claim migrations whose SQL never ran.
        try:
            pg_ctl = _bin("pg_ctl")
        except SystemExit:
            print(
                f"cannot stop the cluster at {target}: `pg_ctl` is not on PATH and "
                "PG_BIN is unset. The data directory is LEFT IN PLACE rather than "
                "removed under a running postmaster. Set PG_BIN to the `bin/` of "
                "the PostgreSQL that started it, then re-run."
            )
            return 1
        try:
            _run([pg_ctl, "-D", str(target), "stop", "-m", "immediate", "-w"])
        except subprocess.CalledProcessError:
            pass  # already stopped; the directory removal below is what matters
        # REMOVE THIS PORT'S CLUSTER, NEVER THE STATE DIRECTORY. This line said
        # `shutil.rmtree(STATE_DIR)`, and on 2026-08-04 `down --port 55471`
        # DESTROYED the port-55432 cluster of a neighbouring session: its
        # postmaster died mid-checkpoint and its data directory went to zero
        # files. Nothing in the repository was lost -- a disposable cluster is
        # reproducible by `up` -- but it was somebody else's running database,
        # and the command that took it named a different port.
        #
        # The defect predates the per-port split and was harmless only while
        # there could be one cluster: STATE_DIR and the cluster were the same
        # thing. Making `--port` real is what gave it reach, so the two changes
        # belong in the same commit.
        shutil.rmtree(target, ignore_errors=True)
        try:
            log_file(port).unlink(missing_ok=True)
        except OSError:
            # An orphaned postmaster can still hold the log open on Windows. That
            # is a stale file, not a reason to fail a teardown that has already
            # removed the cluster -- and raising here left the caller believing
            # `down` had not run at all.
            print(f"note: {log_file(port)} is still held open; left in place")
        print(f"removed {target}")
        if STATE_DIR.exists() and not any(STATE_DIR.iterdir()):
            STATE_DIR.rmdir()  # only when it is genuinely the last one
    else:
        print(f"nothing to remove at {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("up", "down", "env"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    return {"up": up, "down": down, "env": env}[args.action](args.port)


if __name__ == "__main__":
    raise SystemExit(main())
