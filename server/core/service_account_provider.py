"""The provider seam for the service accounts THIS product mints and retires.

WHY A MODULE OF ITS OWN. ``warehouse_tenancy`` decides warehouse NAMES and
mutates one dataset ACL; minting an identity in Google Cloud IAM is a different
responsibility with a different API and a different failure mode, and putting it
there would have made the naming authority import an identity provider.

WHY A SEAM. Exactly like ``mutate_bigquery_dataset_access``: every call to
Google is made through one function, the client object is injectable, and the
Google import is LOCAL so nothing here is imported when the capability is off.
Tests pass a fake session and never reach the network.

WHAT IS NOT MEASURABLE IS NOT INVENTED. "Last read" is not on the service
account resource. Google exposes it through Policy Analyzer's
``serviceAccountLastAuthentication`` activity, an API a deployment may not have
enabled at all -- in which case this module returns ``None`` and the surface
says the reading is not available, rather than printing a date nobody measured.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

_ENABLED_ENV = "TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED"
_IAM_BASE = "https://iam.googleapis.com/v1"
_POLICY_ANALYZER_BASE = "https://policyanalyzer.googleapis.com/v1"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: Google's own constraint on an account id: 6-30 chars, starts with a lowercase
#: letter, then lowercase letters, digits or hyphens.
_ACCOUNT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{5,29}$")

#: The prefix every account this product mints carries, so an operator reading
#: the Google Cloud console can tell ours from one a human created by hand.
ACCOUNT_ID_PREFIX = "toorow-read-"


class ServiceAccountsUnavailable(RuntimeError):
    """The capability is off or unconfigured; no account was created or removed."""


def managed_service_accounts_enabled() -> bool:
    """True when this deployment may mint and retire its own service accounts."""
    return os.environ.get(_ENABLED_ENV, "0").strip().lower() in ("1", "true")


def configured_project() -> str:
    """The GCP project the accounts are minted in -- the same security boundary."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise ServiceAccountsUnavailable(
            "GOOGLE_CLOUD_PROJECT is required before a service account can be created"
        )
    return project


def managed_account_id(grant_id: str) -> str:
    """Return the deterministic account id of the grant *grant_id*.

    Deterministic on purpose: a retry of the same grant asks Google for the SAME
    account instead of minting a second one, and a revocation knows which
    account to delete without storing a second identifier.
    """
    digest = hashlib.sha256(grant_id.encode()).hexdigest()[:18]
    account_id = f"{ACCOUNT_ID_PREFIX}{digest}"
    if not _ACCOUNT_ID_RE.fullmatch(account_id):  # pragma: no cover -- fixed shape
        raise ServiceAccountsUnavailable(f"minted account id is invalid: {account_id!r}")
    return account_id


def managed_account_email(grant_id: str, project: str | None = None) -> str:
    """Return the full address of the account this grant owns."""
    project = project or configured_project()
    return f"{managed_account_id(grant_id)}@{project}.iam.gserviceaccount.com"


def managed_principal(grant_id: str, project: str | None = None) -> str:
    """Return the IAM principal string the dataset ACL will carry."""
    return f"serviceAccount:{managed_account_email(grant_id, project)}"


def _session(client=None):
    """Return the injected client, or an authorized Google session."""
    if client is not None:
        return client
    import google.auth  # noqa: PLC0415 -- local: never imported when capability is off
    from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415

    credentials, _ = google.auth.default(scopes=[_SCOPE])
    return AuthorizedSession(credentials)


def _json(response):
    """Raise on a provider refusal, otherwise return the decoded body."""
    status = getattr(response, "status_code", 200)
    if status >= 400:
        detail = ""
        try:
            detail = str(response.json())
        except Exception:  # noqa: BLE001 -- a body that is not JSON is still detail
            detail = str(getattr(response, "text", ""))
        raise RuntimeError(f"HTTP {status} from Google IAM: {detail}"[:1000])
    try:
        return response.json()
    except Exception:  # noqa: BLE001 -- an empty 200 body is a legal delete answer
        return {}


def create_service_account(grant_id: str, *, display_name: str, client=None) -> dict:
    """Create (or adopt) the account of *grant_id* and return its identity.

    Idempotent by construction: the account id is derived from the grant, so a
    409 ALREADY_EXISTS is the SAME account and is reported as ``created=False``
    rather than raised -- a retried saga must not fail on its own first attempt.
    """
    if not managed_service_accounts_enabled():
        raise ServiceAccountsUnavailable(
            "Managed service accounts are not enabled on this deployment. Enable "
            f"{_ENABLED_ENV}, or bring a principal that already exists."
        )
    project = configured_project()
    account_id = managed_account_id(grant_id)
    session = _session(client)
    response = session.post(
        f"{_IAM_BASE}/projects/{project}/serviceAccounts",
        json={
            "accountId": account_id,
            "serviceAccount": {"displayName": display_name[:100]},
        },
    )
    if getattr(response, "status_code", 200) == 409:
        logger.info("service_account_provider: adopting existing account %s", account_id)
        return {
            "email": managed_account_email(grant_id, project),
            "account_id": account_id,
            "created": False,
        }
    body = _json(response)
    email = body.get("email") or managed_account_email(grant_id, project)
    logger.info("service_account_provider: created %s", email)
    return {"email": email, "account_id": account_id, "created": True}


def delete_service_account(email: str, *, client=None) -> dict:
    """Delete the account *email*; an already-absent account is a success.

    NEVER called before the IAM role has been removed from the dataset: deleting
    the identity first would leave an ACL entry naming a principal nobody can
    audit, which is the opposite of a revocation.
    """
    if not managed_service_accounts_enabled():
        raise ServiceAccountsUnavailable(
            "Managed service accounts are not enabled on this deployment. Enable "
            f"{_ENABLED_ENV} before retiring an account this product created."
        )
    project = configured_project()
    session = _session(client)
    response = session.delete(f"{_IAM_BASE}/projects/{project}/serviceAccounts/{email}")
    if getattr(response, "status_code", 200) == 404:
        return {"email": email, "deleted": False, "absent": True}
    _json(response)
    logger.info("service_account_provider: deleted %s", email)
    return {"email": email, "deleted": True, "absent": False}


def read_last_authentication(email: str, *, client=None) -> str | None:
    """Return the last time *email* authenticated, or None when not measurable.

    ``None`` means "this deployment cannot read it" OR "Google has recorded no
    activity in the retained window". Both are the same thing for the reader --
    no measured date -- and neither is ever rendered as "never used".
    """
    if not managed_service_accounts_enabled():
        return None
    try:
        project = configured_project()
        session = _session(client)
        response = session.get(
            f"{_POLICY_ANALYZER_BASE}/projects/{project}/locations/global/"
            "activityTypes/serviceAccountLastAuthentication/activities:query",
            params={"filter": f'activity.full_resource_name="{email}"'},
        )
        if getattr(response, "status_code", 200) >= 400:
            return None
        activities = (response.json() or {}).get("activities") or []
    except Exception as exc:  # noqa: BLE001 -- an unreadable metric is not an error
        logger.info(
            "service_account_provider: last authentication unreadable for %s (%s)",
            email,
            type(exc).__name__,
        )
        return None
    for activity in activities:
        stamp = (activity.get("activity") or {}).get("lastAuthenticatedTime")
        if stamp:
            return str(stamp)
    return None
