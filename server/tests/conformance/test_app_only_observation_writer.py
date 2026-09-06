"""The `mcp_app` surface has a declarable slot. No fourth effect is needed.

Migration 175 shipped `app.evidence_inspections` with `surface IN ('console',
'mcp_app', 'share')` and recorded, on a constraint comment, that the `mcp_app`
value had no writer because an app-visibility MCP tool "cannot be declared under
the three effects of AD-24 without lying about one of them" -- naming a fourth
effect or an app-only exemption as the way out.

Neither is needed, and this file is the proof rather than the assertion. The
app-only slot has existed since Story 50.6 (`register_profiled(..., app=...)`)
and its rule is `effect="read"` / `confirmation_mode="none"`. `read` is honest
for an observation writer because AD-24's `effect` classifies DOMAIN state and
AD-28 separately REQUIRES every read to leave an append-only audit row; see
`mcp_profiles._record_app_declaration`.

These tests fail if somebody adds a fourth effect, removes the app-only slot, or
makes an observation writer undeclarable again.
"""

from __future__ import annotations

import pytest
from core import mcp_profiles


class _Tool:
    def __init__(self, tool_name, kwargs):
        self.name = tool_name
        self.kwargs = kwargs


class _FakeMcp:
    """Just enough of FastMCP to accept a registration."""

    def __init__(self):
        self.registered: list[_Tool] = []

    def tool(self, handler, **kwargs):
        tool = _Tool(kwargs.get("name") or handler.__name__, kwargs)
        self.registered.append(tool)
        return tool


class _AppConfig:
    def __init__(self, visibility):
        self.visibility = visibility
        self.resource_uri = None


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    """Registering here must not leak into the real catalog's version hash."""
    monkeypatch.setattr(mcp_profiles, "_REGISTRY", type(mcp_profiles._REGISTRY)())
    monkeypatch.setattr(mcp_profiles, "_APP_ONLY_TOOLS", set())


def _register(mcp, *, profile="insights", effect="read", confirmation_mode="none"):
    def record_evidence_inspection(project_id: str, path_id: str) -> dict:
        """The mcp_app writer: one row on app.evidence_inspections, nothing else."""
        return {"recorded": True}

    return mcp_profiles.register_profiled(
        mcp,
        record_evidence_inspection,
        profile=profile,
        effect=effect,
        data_class="operational",
        confirmation_mode=confirmation_mode,
        app=_AppConfig(["app"]),
    )


def test_ad24_still_names_exactly_three_effects():
    """A fourth effect would be an amendment to a ratified decision, not a fix."""
    assert mcp_profiles.EFFECTS == ("read", "prepare", "confirmed_write")


def test_an_app_only_observation_writer_is_declarable_today():
    mcp = _FakeMcp()
    _register(mcp)

    assert [tool.name for tool in mcp.registered] == ["record_evidence_inspection"]
    assert "record_evidence_inspection" in mcp_profiles.app_only_tool_names()
    meta = mcp.registered[0].kwargs["meta"]
    assert meta["effect"] == "read"
    assert meta["confirmation_mode"] == "none"


def test_the_declaration_survives_catalog_validation():
    """Boot must not reject it -- an undeclarable slot is the gap 175 described."""
    mcp = _FakeMcp()
    _register(mcp)

    declarations = mcp_profiles.validate_catalog()
    assert [d.name for d in declarations] == ["record_evidence_inspection"]


@pytest.mark.parametrize(
    ("profile", "effect", "confirmation_mode", "refused_by"),
    [
        # The Insights rule fires first: the default profile is safe reads only.
        ("insights", "confirmed_write", "human", "insights tool"),
        ("insights", "prepare", "none", "insights tool"),
        # Off the default profile, the app-only rule is what refuses.
        ("operations", "confirmed_write", "human", "app-only tool"),
        ("operations", "prepare", "none", "app-only tool"),
    ],
)
def test_the_slot_refuses_the_declarations_that_would_have_lied(
    profile, effect, confirmation_mode, refused_by
):
    """`confirmed_write` demands a ceremony nobody consented to; `prepare` authorizes nothing.

    Both are refused at the registration site under either rule, which is why
    `read` is not merely the convenient answer -- it is the only declarable one.
    """
    mcp = _FakeMcp()
    with pytest.raises(mcp_profiles.CatalogValidationError, match=refused_by):
        _register(mcp, profile=profile, effect=effect, confirmation_mode=confirmation_mode)
    assert mcp.registered == [], "a refused declaration must not reach the server"
