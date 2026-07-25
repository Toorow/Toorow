"""toorow -- Per-project module enablement (Story 7.2, AC2).

Provides the is_module_enabled() helper that checks whether a module is
enabled for a given project by querying app.project_modules.

Design decisions:
  - Default-enabled semantics: a module NOT present in app.project_modules
    for a project is treated as ENABLED by default (P3-dev compatibility).
    The MODULE_DEFAULT_ENABLED env var (default "true") allows flipping to
    opt-in for agency deployments.
  - No in-memory cache: every call queries Postgres. At P3-dev scale
    (1-5 projects, 3-5 modules) this is negligible. Phase B adds a
    short-TTL Redis cache keyed by (project_id, module_name).
  - AD-2: source-agnostic. The module name is a runtime value; no specific
    module name is hard-coded here.
  - The loader registry (LoadedModule) and this enablement table are SEPARATE
    concerns: the loader discovers which modules exist on the filesystem at
    startup; this helper checks which are enabled per project at runtime.
    NEVER conflate them.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_ENABLED_ENV = "MODULE_DEFAULT_ENABLED"


class ModuleEnablementUnavailable(RuntimeError):
    """Raised when a caller requests a fail-closed enablement decision."""


def _get_default_enabled() -> bool:
    """Return the default-enabled sentinel from the env var.

    MODULE_DEFAULT_ENABLED=true  (default) -> modules are enabled when no row exists.
    MODULE_DEFAULT_ENABLED=false           -> modules are disabled when no row exists (opt-in).
    """
    return os.environ.get(_DEFAULT_ENABLED_ENV, "true").lower() != "false"


def is_module_enabled(
    module_name: str, project_id: str, conn, *, fail_closed: bool = False
) -> bool:
    """Return True if the module is enabled for the project.

    Queries app.project_modules for an explicit (project_id, module_name) row.
    If no row exists: returns MODULE_DEFAULT_ENABLED (env var, default True).
    If a row exists: returns the stored enabled flag.

    Args:
        module_name: The module name (kebab-case, as declared in the manifest).
        project_id:  The project identifier.
        conn:        An open psycopg connection (psycopg v3 sync).

    Returns:
        True if the module is enabled (or no row exists and default is True).
        False if the module is explicitly disabled.

    DB errors return the default unless ``fail_closed=True``; strict callers then
    receive :class:`ModuleEnablementUnavailable`.
    """
    default = _get_default_enabled()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled
                FROM app.project_modules
                WHERE project_id = %s AND module_name = %s
                """,
                (project_id, module_name),
            )
            row = cur.fetchone()
            if row is None:
                # No explicit row: module is enabled by default (P3-dev compat).
                return default
            return bool(row[0])
    except Exception as exc:  # noqa: BLE001
        if fail_closed:
            raise ModuleEnablementUnavailable("module enablement could not be verified") from exc
        logger.warning(
            "module_enablement: is_module_enabled: db_error project=%s module=%s: %s",
            project_id,
            module_name,
            exc,
        )
        return default
