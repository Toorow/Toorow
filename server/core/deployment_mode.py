"""toorow -- is this instance ours, or somebody else's?

The two deployments are not the same product surface, and one rule differs
between them: who may create the FIRST organization.

  * HOSTED (toorow Cloud, the default): people arrive through an ENTRY
    invitation issued after a waitlist approval. Accepting it creates no
    membership, and they then create their own organization. Many tenants live
    on one instance.

  * SELF-HOSTED: there is no waitlist, no CRM and nobody to issue an entry
    invitation -- the operator installed the software on their own stack. So the
    instance is CLAIMED instead: the first organization may be created only by an
    email on ``TOOROW_SUPER_ADMINS``, and only while no organization exists yet.
    One instance, one organization (decision Jean, 2026-07-25: « techniquement
    nous pour le self host on le bloque pour le test à 1 »). Everybody else joins
    it through an ordinary organization invitation issued by its owner.

Why this gate has to exist at all: without it a self-hosted instance reachable on
a public URL is claimed by whoever finds the address first and signs in with any
Google account. There is no waitlist standing in front of it.

DEFAULT IS HOSTED, deliberately. An unset or unrecognised value must not
accidentally turn our production into a single-tenant instance, and the failure
mode of guessing wrong in that direction would be silent and total.

ASCII-only, no DB, no import side effects -- mirrors core.super_admin.
"""

from __future__ import annotations

import os

_ENV_VAR = "TOOROW_DEPLOYMENT_MODE"
_SELF_HOSTED_VALUES = frozenset({"self_hosted", "self-hosted", "selfhosted"})


def deployment_mode() -> str:
    """Normalized mode: ``"self_hosted"`` or ``"hosted"`` (the default)."""
    raw = (os.environ.get(_ENV_VAR, "") or "").strip().lower()
    return "self_hosted" if raw in _SELF_HOSTED_VALUES else "hosted"


def is_self_hosted() -> bool:
    """True only when the operator explicitly said so. Read live from the
    environment on every call, like the super-admin allow-list, so tests and
    runtime changes need no restart."""
    return deployment_mode() == "self_hosted"
