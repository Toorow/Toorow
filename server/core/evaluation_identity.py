"""The DECLARED evaluation identity -- neither anonymous, nor a production one.

Ratified 2026-08-24, `docs/product-architecture/analyze-and-test.md`, section
"Decided -- an evaluation run carries a declared evaluation identity". The
decision exists because the two obvious answers are both wrong, and AI-305
measured why:

* **Anonymous.** The eval harness calls the MCP tools in process, with no token,
  so `get_access_token()` returns None and the identity is `anonymous`.
  `resolve_strict_resource_access` refuses it BY NAME (`production_identity_required`),
  `get_report` answers `business_context_state: denied`, and the business-path
  dimension of the corpus is mute forever -- measured against a disposable
  Postgres where the `default` project EXISTS, so it was never a missing row.
* **A production identity.** Handing the harness production access would walk it
  through a door production closes, so an access defect would become invisible
  to the instrument built to see it.

THE THIRD ANSWER, and its two halves are inseparable. The identity is DECLARED
(it authenticates nothing -- it says who the run is), and its admission is bound
to a DECLARED EVALUATION ENVIRONMENT. Fail-closed on both sides:

* an undeclared environment is production (the repository's own default, e.g.
  `connector_activation_api._environment()`), so a deployment that sets nothing
  refuses this identity;
* any other identity is untouched: this module widens nothing for anybody else,
  and the evaluation identity itself still has to be a MEMBER of the
  organization it reads. Admission only carries it past the first door; the
  membership, the org status and the capability floor are all still resolved
  from the database exactly as before. In production there is no such row AND
  the environment refuses it -- two independent reasons, either one sufficient.
"""

from __future__ import annotations

import os

#: The identity an evaluation run declares. Deliberately shaped like a canonical
#: identity (`person_` prefix) so `core.identity_bridge.canonical_identity`
#: returns it unchanged -- an evaluation run must not be silently translated into
#: somebody, and the bridge's rule "what is already canonical does not move"
#: is exactly that guarantee. `EVALUATION` is a placeholder where a ULID sits for
#: a human, and it is not a ULID: nothing can mistake it for a person.
EVALUATION_IDENTITY = "person_EVALUATION"

#: The one environment value that admits `EVALUATION_IDENTITY`.
EVALUATION_ENVIRONMENT = "evaluation"

#: The environment variables the repository already reads, in the order
#: `core.adaptation_executor._production_requires_hard_sandbox` reads them. A
#: SECOND way of naming the environment would be a second authority on what
#: production is, which is the defect this file exists to avoid.
ENVIRONMENT_VARS = ("TOOROW_ENVIRONMENT", "TOOROW_ENV", "ENVIRONMENT", "APP_ENV")

#: What an undeclared environment IS. Not a convenience -- the fail-closed side
#: of the decision, and the same default the connector/inbound APIs take.
DEFAULT_ENVIRONMENT = "production"


def declared_environment() -> str:
    """The environment this process declares, or `production` when it declares none."""
    for name in ENVIRONMENT_VARS:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip().lower()
    return DEFAULT_ENVIRONMENT


def evaluation_environment_declared() -> bool:
    """True only when this process declares itself an evaluation environment."""
    return declared_environment() == EVALUATION_ENVIRONMENT


def is_evaluation_identity(identity: str | None) -> bool:
    """True when *identity* is the declared evaluation identity, whatever the environment."""
    return (identity or "").strip() == EVALUATION_IDENTITY


def evaluation_identity_admitted(identity: str | None) -> bool:
    """May *identity* pass the production-identity door?

    Both halves are required, and neither is a proxy for the other: the identity
    must be the declared one, and the process must declare itself an evaluation
    environment. Everything downstream of this answer is unchanged -- this is a
    door, not a grant.
    """
    return is_evaluation_identity(identity) and evaluation_environment_declared()
