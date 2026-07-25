"""GitHub context connector -- Story 4.5 (Epic 31.4: landing + canonical events).

This module is the first context module in the platform.  It proves that
the module contract (AD-2) is agnostic enough to handle non-marketing, non-KPI
data sources.  Unlike google-analytics and meta-ads, this module:

  * Does NOT produce fact_daily_kpi rows -- it produces app.context_events
    rows in Postgres.
  * Has NO widget (module_kind='context' in manifest; widget_ref absent).
  * Skips conformance Layers 2/3/4; passes Layer 1 and the new Layer 5.

This module is module_kind='context'. It writes context_events, not fact_daily_kpi rows.

Epic 31.4 alignment (non-regression -- behaviour identical, plumbing canonical):
  * Both profiles now declare landing:'context_events' explicitly in manifest.json.
  * Events are persisted via core.context_events.persist_context_event() (canonical
    path) instead of a direct INSERT -- so they go through validate_event_type() and
    receive platform='github', source='github' stamps.
  * Canonical event types: 'release' (releases profile), 'deployment' (deployments
    profile). Both are in dim_event_type.csv (engineering category, diamond marker).
  * module_kind:'context' retained as legacy hint (Epic 31 soft-deprecation).

AD-12: pull() is called by the queue worker only -- no direct GitHub API call
       during an LLM tool invocation.
AD-3:  GitHub PAT/OAuth token obtained from Nango at call time, never stored,
       logged, or passed further.
AD-7:  pull_id minted by core scheduler and passed in; this function never mints its own.
HG-2:  This module MUST NOT write to fact_daily_kpi or any raw_* warehouse table.
HG-3:  Token is obtained immediately before use and discarded -- never persisted.

Auth
----
Nango provider key: github
Supported auth types:
  PAT  -- Personal Access Token with repo read scope.
         Add to Nango as provider github with the PAT as api_key.
         At runtime Nango stores the PAT encrypted; get_fresh_token() returns it.
  OAuth -- Register a GitHub OAuth App; configure GITHUB_CLIENT_ID +
          GITHUB_CLIENT_SECRET in Nango. OAuth grants broader scope.

Connection config fields (stored in Nango connection metadata / connection_ref):
  owner  -- GitHub repository owner (user or org name).
  repo   -- GitHub repository name.

These are retrieved at pull() time via the connection_config argument
(populated from Nango metadata) or via environment variables
GITHUB_OWNER / GITHUB_REPO for local dev.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
# The core does: mcp.mount(loaded.connector_module.mcp_app, namespace=loaded.name)
mcp_app = FastMCP("github")

GITHUB_API_BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _get_github_token(connection_id: str) -> str:
    """Fetch the GitHub PAT/OAuth token from Nango (AD-3).

    The token is used immediately and never stored or logged.
    Nango provider key: 'github'.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    return nango_client.get_fresh_token(connection_id, provider="github")


def _parse_owner_repo(connection_config: dict | None) -> tuple[str, str]:
    """Extract owner/repo from connection_config or env vars (local dev fallback).

    Connection config fields: ``owner``, ``repo`` (stored in Nango metadata).
    Env var fallback for local dev: GITHUB_OWNER / GITHUB_REPO.
    """
    cfg = connection_config or {}
    owner = cfg.get("owner") or os.environ.get("GITHUB_OWNER", "")
    repo = cfg.get("repo") or os.environ.get("GITHUB_REPO", "")
    if not owner or not repo:
        raise ValueError(
            "GitHub owner and repo must be supplied via connection_config "
            "or GITHUB_OWNER/GITHUB_REPO env vars"
        )
    return owner, repo


def _in_date_range(date_str: str, date_from: str, date_to: str) -> bool:
    """Return True when the date portion of *date_str* falls in [date_from, date_to]."""
    if not date_str:
        return False
    event_date = date_str[:10]  # take YYYY-MM-DD prefix
    return date_from <= event_date <= date_to


# ---------------------------------------------------------------------------
# context_events writer (Epic 31.4: delegates to canonical persist_context_event)
# ---------------------------------------------------------------------------


def _write_context_event(
    *,
    project_id: str,
    event_date: str,
    event_type: str,
    label: str,
    description: str | None,
    pull_id: str,
) -> None:
    """Persist one GitHub context event via the canonical core path (Epic 31.4).

    Delegates to core.context_events.persist_context_event() so the event goes
    through validate_event_type() (canonical vocabulary enforcement) and receives
    platform='github' + source='github' stamps. Behaviour is identical to the
    pre-31.4 direct INSERT -- same event_date/type/label/description semantics.

    created_by convention: 'github_pull:<pull_id>' -- machine identity traceable
    back to the scheduler run via pull_id (per Dev Notes, Story 4.5).

    Epic 31: platform='github' (level 1 of platform>type>label identity);
             source='github' distinguishes connector-emitted from manual events;
             value=None (pulse -- no MMM magnitude for releases/deployments yet).
    """
    from core.context_events import persist_context_event  # noqa: PLC0415

    created_by = f"github_pull:{pull_id}"
    persist_context_event(
        project_id=project_id,
        event_date=event_date,
        type=event_type,
        label=label[:120],  # enforce <=120 chars (same guard as before)
        description=description or "",
        created_by=created_by,
        platform="github",
        value=None,
        source="github",
    )


# ---------------------------------------------------------------------------
# pull() -- AD-2 module contract
# ---------------------------------------------------------------------------


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile_id: str = "releases",
    connection_config: dict | None = None,
) -> dict:
    """Fetch GitHub releases or deployments and land them in app.context_events.

    # This module is module_kind='context'. It writes context_events, not fact_daily_kpi rows.
    # AD-12: called by the queue worker only -- no synchronous GitHub API call during
    #         an LLM tool invocation.
    # AD-3:  token obtained immediately before use; falls out of scope after the call.
    # AD-7:  pull_id received from core scheduler -- never minted here.
    # HG-2:  NO writes to fact_daily_kpi or any raw_* warehouse table.

    Parameters
    ----------
    connection_id:
        Nango connection identifier for get_fresh_token().
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the event window.
    project_id:
        Project binding for the context_events rows.
    pull_id:
        Core-minted pull ULID (AD-7).
    profile_id:
        'releases' or 'deployments'.
    connection_config:
        Optional dict with 'owner' and 'repo' keys (from Nango connection metadata).
        Falls back to GITHUB_OWNER / GITHUB_REPO env vars.

    Returns
    -------
    dict
        {"profile_id": str, "rows_written": int, "pull_id": str}

    Raises
    ------
    ValueError
        If owner/repo cannot be resolved.
    RuntimeError
        If the GitHub API returns a non-200 (non-403) response.
    PermissionError
        If the GitHub API returns HTTP 403 (token lacks scope or repo is private).
    """
    owner, repo = _parse_owner_repo(connection_config)

    # AD-3: token obtained immediately before use -- discarded after the call.
    token = _get_github_token(connection_id)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if profile_id == "releases":
        rows_written = _pull_releases(
            owner=owner,
            repo=repo,
            headers=headers,
            date_from=date_from,
            date_to=date_to,
            project_id=project_id,
            pull_id=pull_id,
        )
    elif profile_id == "deployments":
        rows_written = _pull_deployments(
            owner=owner,
            repo=repo,
            headers=headers,
            date_from=date_from,
            date_to=date_to,
            project_id=project_id,
            pull_id=pull_id,
        )
    else:
        raise ValueError(f"Unknown profile_id for github module: {profile_id!r}")

    logger.info(
        "github_pull: pull_id=%s profile=%s rows_written=%d",
        pull_id,
        profile_id,
        rows_written,
    )

    return {
        "profile_id": profile_id,
        "rows_written": rows_written,
        "pull_id": pull_id,
    }


def _pull_releases(
    *,
    owner: str,
    repo: str,
    headers: dict,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
) -> int:
    """Fetch releases from GitHub API and write context_events rows.

    profile_id == 'releases': GET /repos/{owner}/{repo}/releases
    Event fields: type='release', label=tag_name, description=name,
    event_date=published_at::date
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases"
    resp = httpx.get(url, headers=headers, timeout=30.0)

    if resp.status_code == 403:
        raise PermissionError(
            f"GitHub API 403 for releases on {owner}/{repo}: "
            "token lacks repo read scope or repository is private"
        )
    if resp.status_code != 200:
        # Story 25.2: canonical typed error; provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body)

    rows_written = 0
    for release in resp.json():
        published_at = release.get("published_at") or ""
        if not _in_date_range(published_at, date_from, date_to):
            continue

        event_date = published_at[:10]
        tag_name = release.get("tag_name") or ""
        name = release.get("name") or tag_name

        _write_context_event(
            project_id=project_id,
            event_date=event_date,
            event_type="release",
            label=tag_name,
            description=name,
            pull_id=pull_id,
        )
        rows_written += 1

    return rows_written


def _pull_deployments(
    *,
    owner: str,
    repo: str,
    headers: dict,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
) -> int:
    """Fetch deployments from GitHub API and write context_events rows.

    profile_id == 'deployments': GET /repos/{owner}/{repo}/deployments
    Event fields: type='deployment', label=ref+' -> '+environment,
    event_date=created_at::date
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/deployments"
    resp = httpx.get(url, headers=headers, timeout=30.0)

    if resp.status_code == 403:
        raise PermissionError(
            f"GitHub API 403 for deployments on {owner}/{repo}: "
            "token lacks repo read scope or repository is private"
        )
    if resp.status_code != 200:
        # Story 25.2: canonical typed error; provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body)

    rows_written = 0
    for deployment in resp.json():
        created_at = deployment.get("created_at") or ""
        if not _in_date_range(created_at, date_from, date_to):
            continue

        event_date = created_at[:10]
        ref = deployment.get("ref") or ""
        environment = deployment.get("environment") or "unknown"
        label = f"{ref} -> {environment}"

        _write_context_event(
            project_id=project_id,
            event_date=event_date,
            event_type="deployment",
            label=label,
            description=None,
            pull_id=pull_id,
        )
        rows_written += 1

    return rows_written


# ---------------------------------------------------------------------------
# MCP tool -- pull trigger only (no data-returning report tool for context modules)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def github_trigger_pull(
    connection_id: str = "",
    profile_id: str = "releases",
    date_from: str = "",
    date_to: str = "",
    project_id: str = "default",
) -> dict:
    """Trigger a GitHub releases/deployments pull via the queue (AD-12).

    This tool enqueues a pull job -- it does NOT call the GitHub API directly.
    The queue worker will call pull() asynchronously.

    Profiles: releases, deployments
    Output: context_events rows in Postgres (not fact_daily_kpi rows).

    Parameters:
        connection_id: Nango connection identifier for the GitHub integration.
        profile_id: 'releases' or 'deployments'.
        date_from: Start date ISO-8601 (e.g. '2026-07-01').
        date_to:   End date ISO-8601 (e.g. '2026-07-10').
        project_id: Project identifier (default: 'default').

    Returns a job dict with job_id, pull_id, and state (always 'queued').
    """
    from datetime import date, timedelta  # noqa: PLC0415

    # AD-12: enqueue via queue -- no direct HTTP call here.
    from core.queue import enqueue_pull  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=7)).isoformat()

    if not connection_id:
        return {
            "error": "connection_id is required to trigger a GitHub pull",
            "profile_id": profile_id,
        }

    try:
        result = enqueue_pull(
            connection_ref_id=connection_id,
            date_from=date_from,
            date_to=date_to,
            requested_by="github_mcp_tool",
        )
        return {**result, "profile_id": profile_id}
    except Exception as exc:
        return {"error": str(exc), "profile_id": profile_id}


# ---------------------------------------------------------------------------
# Populate verification: this context module opts out of raw-table verification.
# module_kind='context' modules write to app.context_events (Postgres), not to
# a DuckDB raw_* table. Verification.py skips context modules generically when
# the manifest declares module_kind='context'.
#
# Future path (Story 5.x): extend verification.py to count context_events rows
# filtered by pull_id (Option A from Story 4.5 spec).
# ---------------------------------------------------------------------------
