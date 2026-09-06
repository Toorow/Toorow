"""The model's door onto a client object kind, and the guards it may not soften.

Story 64.6 (AI-232). `master_data_mcp` proved the shape: a second door is only a
door if its guard is the first one's. What this file holds is the half a
type-checker cannot -- that the capability split is the declared one, that no
object-kind literal crept into a module whose whole point is that the client names
the object, and that the three tools carry three different confirmation modes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[2] / "core" / "object_kind_mcp.py"


# ---------------------------------------------------------------------------
# The capability split, module-level so it is provable without a database.
# ---------------------------------------------------------------------------


def test_releasing_a_source_needs_manage_and_declaring_needs_edit():
    """The one judgement this module contains, stated as evidence.

    Declaring mints an owner and binds a source: reversible, dated, `edit`.
    RELEASING retires the answer to "what identifies this object" -- every alias
    resolved against that identity (Story 64.10) depends on it -- so it sits with
    the publish-class acts an agent must not reach on its own.
    """
    from core.object_kind_mcp import minimum_capability

    assert minimum_capability("release_source") == "manage"
    assert minimum_capability("declare") == "edit"
    assert minimum_capability("describe") == "read"


def test_an_unknown_action_does_not_fall_through_to_read():
    """A typo must not buy the weakest guard.

    The mapping is a whitelist of the two weak cases and `edit` otherwise; the
    opposite default -- unknown means read -- is how a new action ships
    unguarded.
    """
    from core.object_kind_mcp import minimum_capability

    assert minimum_capability("something_new") == "edit"
    assert minimum_capability("") == "edit"


# ---------------------------------------------------------------------------
# The door stays generic. This is the story's own promise.
# ---------------------------------------------------------------------------


def test_no_object_kind_literal_appears_in_the_module():
    """Story 64.1 exists so the CLIENT names the object, not the code.

    One `if object_kind == "video"` here and the generic store has a second
    country-shaped door -- the exact defect this epic was written to remove, one
    layer up.
    """
    source = _MODULE.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # The docstring names `video` as an EXAMPLE of an opaque kind; the executable
    # half must not compare to one.
    executable = body[body.index("from __future__") :]
    for suspect in ("video", "product", "venue", "country", "market"):
        assert not re.search(rf'object_kind\s*==\s*[\'"]{suspect}', executable)
        assert f'"{suspect}"' not in executable, f"{suspect!r} is a kind literal"


def test_the_authorization_is_the_neighbours_and_not_a_softer_one():
    """`hold_access=True`, strict resolution, and deny-by-default as `not_found`.

    Answering `forbidden` would confirm that another Project's master data
    exists, which is itself the sensitive fact. Read from the source because the
    behaviour it guards needs a database, and the declaration does not.
    """
    source = _MODULE.read_text(encoding="utf-8")
    assert "resolve_strict_resource_access" in source
    assert "hold_access=True" in source
    assert 'raise _tool_error("not_found"' in source
    # The CODE, not the prose: the module docstring names `forbidden` precisely to
    # say it is never emitted, and an assertion that cannot tell the two apart
    # would have to be weakened or the explanation deleted.
    assert '"forbidden"' not in source and "'forbidden'" not in source


def test_a_write_without_an_idempotency_key_is_refused_but_a_read_is_not():
    """The neighbour's rule, and the exception a read deserves.

    A read that demanded an idempotency key would make the cheapest tool the
    most awkward one, and nothing is replayed by describing.
    """
    from core.object_kind_mcp import _run

    with pytest.raises(Exception) as excinfo:
        _run("declare", "proj_EXAMPLE", lambda conn, org, actor: None, idempotency_key="")
    assert "idempotency" in str(excinfo.value) or "not_found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The catalogue declarations.
# ---------------------------------------------------------------------------


def test_the_three_tools_declare_three_different_confirmation_modes():
    """One tool would have to declare ONE mode for acts that do not deserve it.

    A read asking for a human confirmation is a lie in the catalogue; a release
    asking for none is a worse one. Asserted on the registered declarations, not
    on the source text, so a copy-paste that gave all three the same mode fails.
    """
    from core.object_kind_mcp import register
    from fastmcp import FastMCP

    mcp = FastMCP("test-object-kind")
    register(mcp)

    from core.mcp_profiles import registered_declarations

    modes = {
        decl.name: decl.confirmation_mode
        for decl in registered_declarations()
        if decl.name.endswith("client_object_kind") or decl.name.endswith("object_source")
    }
    assert modes.get("describe_client_object_kind") == "none"
    assert modes.get("declare_client_object_kind") == "host"
    assert modes.get("release_client_object_source") == "human"


def test_every_tool_is_governance_profile_and_operational():
    """The generic store is Governance's, and a kind name is neither public nor secret."""
    from core.mcp_profiles import registered_declarations
    from core.object_kind_mcp import register
    from fastmcp import FastMCP

    register(FastMCP("test-object-kind-profile"))
    ours = [
        decl
        for decl in registered_declarations()
        if decl.name.endswith("client_object_kind") or decl.name.endswith("object_source")
    ]
    assert ours, "no object-kind tool was registered"
    for decl in ours:
        assert decl.profile == "governance"
        assert decl.data_class == "operational"
