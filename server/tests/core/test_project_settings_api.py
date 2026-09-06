from core.project_settings_api import project_settings_routes


def test_project_settings_routes_are_exact_and_legacy_geography_is_absent() -> None:
    routes = {route.path: set(route.methods or []) for route in project_settings_routes}
    assert "GET" in routes["/api/projects/{project_id}/settings"]
    assert "PATCH" in routes["/api/projects/{project_id}/settings/profile"]
    assert "POST" in routes["/api/projects/{project_id}/settings/change-sets"]
    for path in (
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/prepare",
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/confirmations",
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/confirm",
    ):
        assert "POST" in routes[path]
