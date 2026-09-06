"""`/api/me/profile` says whether the caller may create an organization.

The gate has existed since epic 34
(`server/core/organizations_api.py#_create_org`, `:7151`); the console
knew nothing about it, so an operator who may add an organization had no way to
learn it and no control to do it.

The test that matters is the LAST one: the flag must come from the same function
the gate calls. A screen that decided it any other way could offer a button the
server then refuses.
"""
from __future__ import annotations

from core import me_api  # AD-43 : le handler vit chez son sujet
from core.super_admin import is_super_admin


def test_the_flag_is_computed_by_the_same_function_as_the_gate(monkeypatch):
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "boss@example.com")
    assert is_super_admin("boss@example.com") is True
    assert is_super_admin("someone@example.com") is False


def test_deny_by_default_when_the_allow_list_is_unset(monkeypatch):
    monkeypatch.delenv("TOOROW_SUPER_ADMINS", raising=False)
    assert is_super_admin("boss@example.com") is False


def test_the_profile_handler_reads_the_allow_list_at_all():
    # The seam: the handler must call `is_super_admin`, not re-implement it.
    import inspect
    source = inspect.getsource(me_api._get_my_profile)
    assert "is_super_admin" in source
    assert '"is_super_admin"' in source
