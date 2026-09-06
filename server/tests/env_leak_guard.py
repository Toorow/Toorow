"""One module must not decide where the NEXT module's database lives (AI-291).

THE MEASUREMENT THIS EXISTS FOR. On 2026-09-01 a full run of `server/tests/core`
reported 69 failures and 88 errors. Roughly 115 of them had ONE cause:
`test_render_shares_api.py` called

    os.environ.pop("PLATFORM_DB_URL", None)

to prove a refusal, and never put it back. Every module collected after it fell
through to psycopg's default of `localhost:5432`, where nothing listens, and
reported `OperationalError` -- as a defect of the code under test. The finder
was a human reading 115 tracebacks looking for the one that was not a
consequence.

WHY A RUNTIME GUARD AND NOT A GREP. `os.environ.pop` is not the defect; a pop
that is restored in a `finally` is correct, and `monkeypatch.delenv` is a pop
too. What is wrong is a MODULE THAT ENDS WITH A DIFFERENT ENVIRONMENT THAN IT
STARTED WITH, whatever wrote it -- a bare `os.environ[...] = ...`, a fixture
that forgot its teardown, a helper called at import time. Only running it can
tell, so this is a module-scoped autouse fixture and not a static walk.

WHAT IS WATCHED, and why it is not everything. Three families, because these are
the ones whose loss is INVISIBLE at the point of failure: the module that trips
over them reports a connection error or a refusal that belongs to somebody else.

* `PLATFORM_DB_URL` -- the platform database. Losing it is the 115-failure
  cascade above, and psycopg answers a plausible-looking error rather than
  "somebody removed your DSN".
* `TEST_POSTGRES_DSN`, `TEST_POSTGRES_OWNER_DSN` -- what `live_postgres` reads to
  decide whether to SKIP. Losing one turns a pg-gated suite green by skipping it,
  which is worse than red.
* `TOOROW_*` -- every product lever: the auth mode, the warehouse mode, the DuckDB
  path, the QA write permission. A module that leaves `TOOROW_AUTH_MODE` behind
  decides the access-control answers of every module after it.

IT NAMES, IT DOES NOT REPAIR. Restoring the value here would hide the leak and
make this file the owner of an environment nobody declared. The failure names the
MODULE, the KEY and both values, and the repair is always the same one: use
`monkeypatch.setenv` / `monkeypatch.delenv`, which pytest restores by contract.

WHAT IT FOUND ON ITS FIRST FULL RUN, and it is the argument for the whole file.
Twenty pg-gated modules, 339 passing tests, not one red: and
`test_epic36_e2e_first_journey.py` walked out leaving `TOOROW_INVITATION_PEPPER`,
`TOOROW_HANDOFF_PEPPER` and both console origins set to its own fixture values.
Nothing in that module failed. Nothing downstream failed either -- on that
ordering, on that day. The next module to sign an invitation would have signed it
with an e2e test pepper and read the mismatch as its own defect, which is the
exact shape of the 115 failures above.
"""

from __future__ import annotations

import os

#: Exact keys whose disappearance is read by another module as its own failure.
WATCHED_KEYS = ("PLATFORM_DB_URL", "TEST_POSTGRES_DSN", "TEST_POSTGRES_OWNER_DSN")

#: Every product lever is namespaced. One prefix, not a list to keep up to date.
WATCHED_PREFIX = "TOOROW_"


def _watched(name: str) -> bool:
    return name in WATCHED_KEYS or name.startswith(WATCHED_PREFIX)


def snapshot() -> dict[str, str]:
    """The watched half of the environment, right now."""
    return {name: value for name, value in os.environ.items() if _watched(name)}


def differences(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """One sentence per key the module changed, in a stable order."""
    changed: list[str] = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was == now:
            continue
        if now is None:
            changed.append(f"{name}: removed (it held a value when the module started)")
        elif was is None:
            changed.append(f"{name}: set to {now!r}, and left set")
        else:
            changed.append(f"{name}: changed from {was!r} to {now!r}")
    return changed


def report(module_id: str, before: dict[str, str], after: dict[str, str]) -> str:
    """The failure text, or `""` when the module gave the environment back."""
    changed = differences(before, after)
    if not changed:
        return ""
    return (
        f"{module_id} changed the environment and did not put it back. Every module "
        f"collected after it reads these values:\n  "
        + "\n  ".join(changed)
        + "\n\nUse `monkeypatch.setenv` / `monkeypatch.delenv` -- pytest restores them "
        "by contract. A bare `os.environ.pop` in a test cost ~115 downstream failures "
        "on 2026-09-01, none of which named the module that caused them."
    )
