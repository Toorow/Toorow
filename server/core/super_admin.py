"""toorow -- super-admin allow-list (Story 34.3).

Deny-by-default gate for the ORG-PLAN control surface (the single admin->prod
write of Epic 34). The allow-list is an environment variable so it is set per
deployment and never hard-coded in the repo:

    TOOROW_SUPER_ADMINS="alice@toorow.io,bob@toorow.io"

Contract:
  * is_super_admin(email) -> bool
  * Empty / unset env var => NOBODY is a super-admin (deny-by-default).
  * Comparison is case-insensitive and whitespace-trimmed on both sides.
  * An empty / None email is never a super-admin.

The caller (org_plan_api) turns a non-super-admin into a 404 (not 403): we do
NOT reveal that the control surface exists to a caller who is not allow-listed.

ASCII-only strings (Windows/CI safe). No DB, no import side effects.
"""

from __future__ import annotations

import os

_ENV_VAR = "TOOROW_SUPER_ADMINS"


def _allow_list() -> frozenset[str]:
    """Parse TOOROW_SUPER_ADMINS into a normalized set of emails.

    Read live from the environment on every call so tests (and runtime plan
    changes) do not need a process restart. Deny-by-default: unset or blank
    => empty set.
    """
    raw = os.environ.get(_ENV_VAR, "") or ""
    return frozenset(
        part.strip().lower() for part in raw.split(",") if part.strip()
    )


def is_super_admin(email: str | None) -> bool:
    """Return True iff *email* is in the TOOROW_SUPER_ADMINS allow-list.

    Deny-by-default: returns False for None, empty string, or when the env var
    is unset/blank. Case-insensitive, whitespace-trimmed.
    """
    if not email:
        return False
    return email.strip().lower() in _allow_list()
