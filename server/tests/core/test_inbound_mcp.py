"""Story 38.15: inbound operations from MCP, on the console's own reads.

The two assertions that matter here are structural, not behavioural:

  1. THERE IS NO COMMIT TOOL. AC4 requires a noninteractive host to inspect and
     prepare but never to authorize. A guard on a commit tool can be argued
     around by a sufficiently persuasive prompt; a tool that does not exist
     cannot. This file fails if one is ever added.
  2. EVERY TOOL CALLS THE FUNCTION THE REST HANDLER CALLS. AC2 asks for
     semantically identical state between console and MCP, and two surfaces can
     only be guaranteed identical if there is one implementation. This is
     checked by source inspection, because a behavioural test would pass just as
     happily against a second, drifting query.

Plus the ordinary ones: scope refusals are indistinguishable from absence, and
nothing sensitive crosses the model channel.
"""

from __future__ import annotations

import inspect

import pytest

from tests.support.navigation_source import navigation_source

# ---------------------------------------------------------------------------
# (1) The shape of the surface.
# ---------------------------------------------------------------------------


def test_the_registered_tools_are_exactly_five_reads_and_two_preparations():
    """La liste est EXACTE, et c est le point : y ajouter un outil doit faire
    rougir un test avant d expedier quoi que ce soit.

    `get_inbound_mapping_context` a ete ajoute le 2026-08-08 pour 38.16 AC5 --
    console et MCP doivent lire le meme contexte de mapping. C est une LECTURE :
    elle rend `governed_path`, qui NOMME le moteur de changement et ses URL
    console, et ne les appelle pas.

    `test_inbound_routing` a ete ajoute le 2026-08-09 pour 38.15 AC5 -- le test
    synthetique au NIVEAU DATASTREAM. Il n ecrit rien : ni recu, ni evidence
    brute, ni execution, ni publication. La seule trace qu il laisse est un
    evenement de cadencement, delibere et explique dans `inbound_routing_test`.

    `prepare_inbound_reprocess_scope` a ete ajoute le 2026-08-16 (AI-281) -- la
    PORTEE gouvernee n etait atteignable que par la route REST, donc un agent
    pouvait rejouer une piece et pas un ensemble nomme. C est une PREPARATION,
    comme sa soeur unitaire : elle lit, re-hache et ENUMERE ses membres, et elle
    ne commite rien. Il n y a toujours aucun outil d execution ici, et pour la
    meme raison -- executer peut deplacer le pointeur publie.

    L absence d outil de commit reste entiere, et
    `test_no_tool_in_this_module_can_commit` la tient independamment de cette
    liste.
    """
    from core.inbound_mcp import INBOUND_MCP_TOOLS

    assert set(INBOUND_MCP_TOOLS) == {
        "get_inbound_health",
        "list_inbound_attachments",
        "get_inbound_delivery",
        "get_inbound_mapping_context",
        "prepare_inbound_reprocess",
        "prepare_inbound_reprocess_scope",
        "test_inbound_routing",
    }


def test_the_mapping_context_tool_calls_the_function_the_rest_handler_calls():
    """L invariant que ce module declare en tete : une seule implementation.

    << AC2 asks for semantically identical state between console and MCP, and
    the only way two surfaces cannot drift is if there is one implementation. >>
    Un outil qui referait la requete a sa facon satisfairait la signature et
    perdrait la redaction qui vit dans la fonction.
    """
    from unittest.mock import patch

    import core.inbound_mcp as mod

    sentinel = {"available": True, "reason": None, "raw_import_id": "inbraw_1"}
    with (
        patch.object(mod, "_readable_datastream", return_value=(_FakeConn(), "proj_1")),
        patch(
            "core.inbound_mapping_entry.get_mapping_repair_context",
            return_value=sentinel,
        ) as shared,
    ):
        out = mod.get_inbound_mapping_context("ds_1", "inbraw_1")

    assert out is sentinel
    assert shared.call_args.kwargs["raw_import_id"] == "inbraw_1"
    assert shared.call_args.kwargs["datastream_id"] == "ds_1"


def test_the_mapping_context_tool_refuses_a_datastream_outside_the_caller_scope():
    """AD-5 : un refus a la MEME forme qu une ressource absente, sinon on
    enumere les Datastreams d autrui en regardant qui repond differemment."""
    from unittest.mock import patch

    import core.inbound_mcp as mod

    with patch.object(mod, "_readable_datastream", return_value=None):
        out = mod.get_inbound_mapping_context("ds_autre", "inbraw_1")

    assert out == mod._denied("not_readable")


class _FakeConn:
    def close(self):
        return None


def test_no_tool_in_this_module_can_commit():
    """The load-bearing assertion of Story 38.15 AC4.

    If someone adds `execute_inbound_reprocess` or any other mutating tool to
    this module, this fails -- which is the intended cost of introducing a
    model-invokable write to inbound data.
    """
    import core.inbound_mcp as mod

    forbidden = ("execute", "commit", "confirm", "apply", "rotate", "revoke", "delete")
    offenders = [
        name for name in mod.INBOUND_MCP_TOOLS if any(word in name.lower() for word in forbidden)
    ]
    assert offenders == [], (
        f"a mutating MCP tool was added to the inbound surface: {offenders}. "
        f"A reprocess is confirmed in the console, not over this channel."
    )

    # And the module must not import the executor at module level either.
    source = inspect.getsource(mod)
    assert "execute_reprocess" not in source.split("prepare_reprocess")[0], (
        "the executor is referenced before the preparation -- check no tool calls it"
    )


def test_the_tools_are_actually_registered_with_the_mcp_app():
    """A tool defined and never registered is not a surface."""
    from core.inbound_mcp import INBOUND_MCP_TOOLS

    registered: list[str] = []

    class _FakeMcp:
        # `**_declaration` because AD-43 made every registration carry its profile:
        # `register_profiled` forwards name/tags/meta to `mcp.tool`. A recorder with
        # the old one-argument signature does not fail this assertion -- it raises a
        # TypeError inside the registrar, which is a different and less useful red.
        def tool(self, fn, **_declaration):
            registered.append(fn.__name__)
            return fn

    from core import mcp_profiles
    from core.inbound_mcp import register_inbound_tools

    before = dict(mcp_profiles._REGISTRY.declarations)
    try:
        register_inbound_tools(_FakeMcp())
    finally:
        mcp_profiles._REGISTRY.declarations.clear()
        mcp_profiles._REGISTRY.declarations.update(before)
    assert sorted(registered) == sorted(INBOUND_MCP_TOOLS)


def test_main_registers_the_inbound_tools():
    """The registration line in core.main is part of the contract, not a detail."""
    import core.main as main_mod

    source = inspect.getsource(main_mod)
    assert "register_inbound_tools" in source, (
        "core.main no longer registers the inbound tools -- the MCP surface is silently gone"
    )


# ---------------------------------------------------------------------------
# (2) One implementation, not two.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, expected_call",
    [
        ("get_inbound_health", "get_inbound_health"),
        ("list_inbound_attachments", "get_attachment_inbox"),
        ("get_inbound_delivery", "get_delivery_timeline"),
        ("get_inbound_mapping_context", "get_mapping_repair_context"),
        ("prepare_inbound_reprocess", "prepare_reprocess"),
    ],
)
def test_each_tool_delegates_to_the_shared_read(tool_name, expected_call):
    """No tool builds its own query.

    Checked in the source rather than by behaviour: a behavioural test passes
    just as happily against a second, subtly different query, which is exactly
    the drift AC2 forbids.
    """
    import core.inbound_mcp as mod

    source = inspect.getsource(getattr(mod, tool_name))
    assert expected_call in source, (
        f"{tool_name} does not call {expected_call} -- a second implementation "
        f"means console and MCP can disagree about the same delivery"
    )
    # And it must not be reaching into SQL on its own.
    assert "SELECT " not in source or "org_id" in source, (
        f"{tool_name} issues its own SELECT beyond the org lookup"
    )


def test_no_tool_reaches_the_import_ledger_directly():
    """The ledger is read through the health module, which redacts."""
    import core.inbound_mcp as mod

    source = inspect.getsource(mod)
    assert "managed_feed_import_ledger" not in source


# ---------------------------------------------------------------------------
# (3) Refusals disclose nothing.
# ---------------------------------------------------------------------------


def _deny_scope(monkeypatch):
    import core.inbound_mcp as mod

    monkeypatch.setattr(mod, "_readable_datastream", lambda ds: None)


def test_an_unreadable_datastream_returns_the_same_shape_everywhere(monkeypatch):
    """ "You may not" and "it does not exist" must be one answer.

    Distinguishing them lets a caller enumerate other tenants' Datastreams by
    comparing which ones answer differently.
    """
    import core.inbound_mcp as mod

    _deny_scope(monkeypatch)

    answers = [
        mod.list_inbound_attachments("ds-someone-else"),
        mod.get_inbound_delivery("ds-someone-else", "inbrx_1"),
        mod.prepare_inbound_reprocess("ds-someone-else", "inbraw_1"),
        mod.get_inbound_health("ds-someone-else"),
    ]
    for answer in answers:
        assert answer["available"] is False
        assert answer["reason"] == "not_readable"
    # Byte-identical, so no timing-free signal distinguishes the two cases.
    assert len({str(sorted(a.items())) for a in answers}) == 1


def test_a_refusal_names_no_project_org_or_caller_value(monkeypatch):
    """VALUES, not words.

    The refusal prose legitimately contains the word "identity" -- it explains
    what was checked. What must never appear is an actual project id, org id or
    caller value, which is what a probe would harvest. The first version of this
    assertion matched the generic sentence and would have forced the message to
    be made less clear for no security gain.
    """
    import core.inbound_mcp as mod

    _deny_scope(monkeypatch)
    monkeypatch.setattr(mod, "_caller_identity", lambda: "user-42@example.com")
    blob = str(mod.list_inbound_attachments("ds-1"))

    for value in ("proj_", "org_", "user-42@example.com"):
        assert value not in blob


# ---------------------------------------------------------------------------
# (4) The preparation carries its continuation.
# ---------------------------------------------------------------------------


def test_the_proposal_says_it_cannot_commit_and_where_it_completes(monkeypatch):
    import core.inbound_mcp as mod
    import core.inbound_reprocess as ir

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr(mod, "_readable_datastream", lambda ds: (_Conn(), "proj-1"))
    monkeypatch.setattr(
        ir,
        "prepare_reprocess",
        lambda conn, **kw: {"availability": {"available": True}, "rollback": "..."},
    )

    out = mod.prepare_inbound_reprocess("ds-1", "inbraw_1")

    assert out["commit_available_here"] is False
    assert out["why_not_here"]
    console = out["console"]
    assert console["kind"] == "console"
    assert console["requires_authenticated_session"] is True
    # A semantic destination, never a hard-coded URL: a literal path would
    # freeze a route this module does not own.
    assert "http" not in str(console).lower()
    assert console["owner_reference"]["object_id"] == "ds-1"


def test_mcp_attachment_evidence_matches_the_authorized_reader_projection(monkeypatch):
    import core.inbound_health as health
    import core.inbound_mcp as mod

    class Conn:
        def close(self):
            return None

    safe_item = {
        "raw_import_id": "inbraw_1",
        "scan_verdict": {
            "accepted": False,
            "reason": "scan_time_budget_exceeded",
            "malware": "unavailable",
            "policy": {"version": "inbound-scan-policy-v2", "max_scan_seconds": 30},
            "evidence": {"size_bytes": 42, "size_limit": 100},
        },
        "scan_job": {
            "state": "DEAD_LETTER",
            "attempt_count": 5,
            "max_attempts": 5,
            "error_code": "scan_time_budget_exceeded",
            "recovery": {"command": "retry_inbound_scan_job", "requires_authorization": True},
        },
    }
    monkeypatch.setattr(mod, "_readable_datastream", lambda value: (Conn(), "proj-1"))
    monkeypatch.setattr(health, "get_attachment_inbox", lambda *args, **kwargs: [safe_item])
    answer = mod.list_inbound_attachments("ds-1")
    assert answer["items"][0]["scan_verdict"] == safe_item["scan_verdict"]
    assert answer["items"][0]["scan_job"] == safe_item["scan_job"]


def test_the_two_doors_answer_the_same_thing_on_the_same_page_bounds(monkeypatch):
    """Une commande, une reponse -- quelle que soit la porte empruntee.

    L'outil MCP bornait l'entree en silence (`max(1, min(limit, 200))`) la ou la
    porte REST laisse le lecteur refuser et rend `400 invalid_param`. Le meme
    appel avait donc deux issues selon le chemin : servir 200 lignes a qui en
    demande 500 sans le lui dire d'un cote, refuser de l'autre.

    Le lecteur EST l'autorite sur ses bornes (`inbound_health.py:762-765` leve
    `ValueError`). Les deux portes le laissent decider et rapportent son refus.
    """
    from core import inbound_mcp

    class _Conn:
        def close(self):
            pass

    # `_readable_datastream` rend le couple (connexion, projet) : la doublure
    # rend exactement ce couple, jamais une forme inventee.
    monkeypatch.setattr(
        inbound_mcp, "_readable_datastream", lambda ds: (_Conn(), "proj_EXAMPLE")
    )

    out = inbound_mcp.list_inbound_attachments("ds_1", limit=500)

    # Refuse, et nomme la meme cause que la porte REST -- jamais servi en silence.
    assert out["available"] is False
    assert out["reason"] == "invalid_param"


def test_the_console_reference_is_one_the_router_would_actually_open():
    """Une adresse qu'aucune console ne peut ouvrir se lit pourtant comme un lien.

    `ContentRouter` jette en silence une reference dont la section n'existe pas,
    ou dont l'`object_type` n'est pas declare par cette section
    (`ui/admin/src/shell/ContentRouter.tsx`, la resolution d owner). Trois erreurs se cumulaient
    et chacune suffisait : `surface: "workspace"` (le contrat n'admet que
    `project` ou `global`), la cle `object` au lieu d'`object_type`, et la section
    `imports` -- qui ne declare que `type: "import"`, jamais `datastream`.

    Ce test lit `navigation.ts`, pas une copie : la forme emise doit designer une
    section reelle, un type d'objet que cette section declare, et un onglet que
    ce type porte. Un renommage cote console fait rougir ici.
    """
    import re

    from core.inbound_mcp import _console_reference

    owner = _console_reference("ds_EXAMPLE", section="data")["owner_reference"]
    assert owner["surface"] in {"project", "global"}
    assert owner["object_type"] == "datastream"

    navigation = navigation_source()

    # La section existe, et elle declare CE type d'objet avec CET onglet.
    start = navigation.find(f'section("{owner["section"]}"')
    assert start >= 0, f"section {owner['section']!r} absente de navigation.ts"
    # Jusqu'a la section suivante : le bloc entier, sans dependre d'un crochet
    # interne (`tabs: [...]` en contient un, et s'y arreter lisait la moitie).
    following = navigation.find('section("', start + 1)
    declared = navigation[start : following if following > 0 else start + 800]
    assert f'type: "{owner["object_type"]}"' in declared
    tabs = re.search(r"tabs: \[(.*?)\]", declared, re.S)
    assert tabs and f'"{owner["tab"]}"' in tabs.group(1), (
        f"l'onglet {owner['tab']!r} n'est pas declare par {owner['object_type']!r}"
    )
