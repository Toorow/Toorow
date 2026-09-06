"""Story 36.1 strict admin/API authorization seam tests."""

import json
from unittest.mock import MagicMock


def test_strict_connection_scope_denial_is_nondisclosing(monkeypatch):
    from core import connections_api, db, project_access

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("proj-1", "google-ads")
    conn.cursor.return_value = cur
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_a, **_k: project_access.AccessDecision(False, "grant_required"),
    )
    local_context = MagicMock()
    monkeypatch.setattr(db, "set_local_access_context", local_context)
    audit = MagicMock()
    monkeypatch.setattr(connections_api, "write_audit_row", audit)

    scope, response = connections_api._resolve_conn_project_scoped(
        "conn-1", "member-1", conn
    )

    assert scope is None
    assert response.status_code == 404
    # The refusal stays NON-DISCLOSING and now names a gesture (the refusal
    # ratchet covers `connections_api.py` since 2026-08-31): it says the
    # connection is not readable under this sign-in, never that it exists.
    body = json.loads(response.body)
    assert body["code"] == "not_found"
    assert body["message"] == (
        "This connection is not readable under your sign-in. Choose a connection "
        "from a Project you have access to, or ask an administrator of this "
        "organisation to grant it."
    )
    audit.assert_called_once()
    # Story 21.6: this helper borrows a connection, it no longer arms one. The
    # floor is installed by the acquisition -- see the test below, which is what
    # now holds the invariant this line used to hold.
    local_context.assert_not_called()


def test_every_caller_of_the_scoped_resolver_acquires_an_armed_connection():
    """The helper stopped arming, so the acquisitions must carry it -- all of them.

    Without this, removing the arming from `_resolve_conn_project_scoped` would
    silently lower the floor for any handler that kept opening a bare
    `get_connection()`, and nothing would say so.
    """
    import inspect
    import re

    # AD-43 : les appelants de `_resolve_conn_project_scoped` ont rejoint le
    # module de leur sujet. Le JOINT reste dans `admin_api` -- c'est de
    # l'autorisation -- mais ses APPELS sont ailleurs, et un garde qui ne lit
    # que le joint ne voit plus aucun appel : il passerait vert sur zero ligne.
    from core import admin_api, connections_api

    source = (
        inspect.getsource(admin_api).splitlines()
        + inspect.getsource(connections_api).splitlines()
    )
    unarmed = []
    for number, line in enumerate(source, start=1):
        if "_resolve_conn_project_scoped(" not in line or "def " in line:
            continue
        acquisition = next(
            (source[j] for j in range(number - 2, max(0, number - 12), -1)
             if "as conn:" in source[j]),
            "",
        )
        if not re.search(r"with request_connection\(", acquisition):
            unarmed.append(
                f"{number}: {line.strip()}  <- "
                f"{acquisition.strip() or 'no acquisition found'}"
            )
    assert not unarmed, (
        "handler(s) resolving a connection scope on an UNARMED connection:\n  "
        + "\n  ".join(unarmed)
    )


def test_strict_connection_scope_allows_explicit_project_decision(monkeypatch):
    from core import connections_api, db, project_access

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("proj-1", "google-ads")
    conn.cursor.return_value = cur
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_a, **_k: project_access.AccessDecision(
            True, "explicit_grant", "view", "org-1"
        ),
    )
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())

    scope, response = connections_api._resolve_conn_project_scoped(
        "conn-1", "member-1", conn
    )

    assert response is None
    assert scope == {"project_id": "proj-1", "provider": "google-ads"}
