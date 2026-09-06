from pathlib import Path

from core.getting_started_api import getting_started_routes
from core.project_access_api import project_access_routes

ROOT = Path(__file__).resolve().parents[2]


def _paths(routes):
    return {route.path for route in routes}


def test_canonical_global_scope_api_paths_are_registered():
    assert _paths(project_access_routes) == {
        "/api/projects/{project_id}/access",
        "/api/projects/{project_id}/access/grant-changes",
        "/api/projects/{project_id}/access/grant-changes/{change_id}/confirmations",
        "/api/projects/{project_id}/access/grant-changes/{change_id}/confirm",
        "/api/projects/{project_id}/access/handoffs",
    }
    assert _paths(getting_started_routes) == {
        "/api/projects/{project_id}/getting-started",
        # Amendment of 2026-08-17: the GET reads and derives, and this POST is the
        # explicit, idempotent gesture that creates the journey and journals it.
        # A read that creates made the first VIEWER of the page its operator.
        "/api/projects/{project_id}/getting-started/journey",
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/handoffs",
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/owner",
    }


def test_production_code_has_no_default_open_project_authority_or_obsolete_route():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "core").glob("*.py"))
    forbidden = (
        "app.project_members",
        "identity_has_project_access",
        "resolve_project_role",
        "TOOROW_EPIC36_PRODUCTION_ENABLED",
        "/onboarding/responsibilities",
        "/setup-journey",
    )
    assert all(item not in source for item in forbidden)
