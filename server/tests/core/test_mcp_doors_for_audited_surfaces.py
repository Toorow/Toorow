"""Chantier 67-23 -- les quatre portes MCP que l'audit du 2026-08-17 a trouvees absentes.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST CELA. Chacune de ces portes a ete
construite sur une promesse unique : **elle compose par la MEME fonction que
l'ecran**. C'est la seule propriete qui empeche qu'un agent et une personne
finissent par ne pas etre d'accord sur ce que ce projet collecte, sur ce que son
juge a conclu, ou sur quel flux fait foi pour un total. Une promesse pareille ne
se verifie pas en lisant le code : elle se verifie en remplacant le composeur par
une doublure et en exigeant que l'outil l'ait appele, avec les arguments que la
console lui passe.

AUCUN POSTGRES. Ce qui est sous test n'est pas la lecture -- chaque composeur a
ses propres tests, pg-gated -- mais le CHAINAGE : garde, puis composeur partage,
puis enveloppe AD-1. La garde de portee a son harnais a elle
(`tests/isolation/test_mcp_tool_scope_refusal.py`) et n'est pas redite ici ; elle
est neutralisee pour que ces tests mesurent ce qui vient apres.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

IDENTITY = "person_tester"
PROJECT = "proj_EXAMPLE"


def _tool(module_name: str, tool_name: str):
    """Le handler qu'un module enregistre, capte a l'enregistrement.

    Meme patron que `tests/isolation/test_mcp_tool_scope_refusal._tool_registered_by` :
    ces outils sont des fonctions LOCALES de `register(mcp)`, il n'existe aucun
    attribut de module a appeler.
    """
    import importlib

    import core.mcp_profiles as profiles

    module = importlib.import_module(f"core.{module_name}")
    captured: dict[str, object] = {}

    def _record(_mcp, handler, **_kwargs):
        captured[handler.__name__] = handler
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        module.register(object())
    finally:
        profiles.register_profiled = original
    return captured[tool_name]


def _declaration(module_name: str, tool_name: str) -> dict:
    """La declaration `register_profiled` d'un outil, telle qu'elle est posee."""
    import importlib

    import core.mcp_profiles as profiles

    module = importlib.import_module(f"core.{module_name}")
    seen: dict[str, dict] = {}

    def _record(_mcp, handler, **kwargs):
        seen[handler.__name__] = kwargs
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        module.register(object())
    finally:
        profiles.register_profiled = original
    return seen[tool_name]


@pytest.fixture()
def a_holder(monkeypatch):
    """Une identite qui A le projet, et une connexion qui s'ouvre.

    La garde est neutralisee ICI et seulement ici : son comportement est prouve
    par un appel refuse dans le harnais d'isolation, et le redire ici mesurerait
    deux fois la meme chose en laissant le chainage non mesure.
    """
    import core.db as core_db
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)
    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


def _payload(result) -> dict:
    """Le `data` de l'enveloppe AD-1 rendue par un outil."""
    return result.structured_content["data"]


def _refusal(excinfo) -> dict:
    return json.loads(str(excinfo.value))


# ---------------------------------------------------------------------------
# 1. La lentille Data -- rapport d'audit 04.
# ---------------------------------------------------------------------------


def test_the_data_door_composes_through_the_screens_own_function(a_holder, monkeypatch):
    """LA promesse : `compose_data_surface`, appelee avec les memes arguments.

    Une seconde derivation ici et l'ecran Data et l'agent finiraient par
    diverger sur le nombre d'imports atterris -- exactement le defaut que
    l'audit du meme jour a trouve ailleurs.
    """
    import core.data_surface as data_surface

    seen: dict = {}

    def _compose(project_id, lens, conn, **kwargs):
        seen.update({"project_id": project_id, "lens": lens, "conn": conn, **kwargs})
        return {"items": [], "unavailable_reasons": [{"message": "Nothing landed yet."}]}

    monkeypatch.setattr(data_surface, "compose_data_surface", _compose)
    result = _tool("data_surface_mcp", "get_data_surface")(
        project_id=PROJECT, lens="imports"
    )

    assert seen["project_id"] == PROJECT
    assert seen["lens"] == "imports"
    assert seen["conn"] is not None
    # can_edit=False, TOUJOURS : la porte lit. Un `True` ici ferait annoncer par
    # `allowed_actions` des gestes que cet outil ne porte pas.
    assert seen["can_edit"] is False
    assert _payload(result) is not None


def test_an_empty_data_lens_says_why_in_the_composers_own_words(a_holder, monkeypatch):
    """Un etat vide honnete, et pas une seconde phrase de vide inventee ici."""
    import core.data_surface as data_surface

    monkeypatch.setattr(
        data_surface,
        "compose_data_surface",
        lambda *a, **k: {
            "items": [],
            "unavailable_reasons": [
                {"code": "imports_evidence_empty", "message": "No owned import evidence."}
            ],
        },
    )
    result = _tool("data_surface_mcp", "get_data_surface")(project_id=PROJECT, lens="imports")
    assert "No owned import evidence." in result.content[0].text


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"project_id": "", "lens": "imports"}, "missing_param"),
        ({"project_id": PROJECT, "lens": "invoices"}, "unknown_lens"),
        ({"project_id": PROJECT, "lens": "overview", "object_id": "x"}, "no_object_detail"),
    ],
)
def test_the_data_door_refuses_before_it_opens_anything(a_holder, kwargs, code):
    """Ces trois refus parlent de l'ARGUMENT, donc ils tombent avant la garde.

    Un refus de forme ne dit rien du projet du voisin : il parle de ce que
    l'appelant a lui-meme envoye. Les faire tomber apres la garde ne les
    rendrait pas plus surs et ferait payer une connexion pour rien.
    """
    with pytest.raises(ToolError) as excinfo:
        _tool("data_surface_mcp", "get_data_surface")(**kwargs)
    assert _refusal(excinfo)["code"] == code


def test_a_missing_data_object_is_named_and_not_swallowed(a_holder, monkeypatch):
    import core.data_surface as data_surface

    def _raise(*_a, **_k):
        raise data_surface.DataObjectNotFound("nope")

    monkeypatch.setattr(data_surface, "compose_data_surface", _raise)
    with pytest.raises(ToolError) as excinfo:
        _tool("data_surface_mcp", "get_data_surface")(
            project_id=PROJECT, lens="sources", object_id="src_x"
        )
    assert _refusal(excinfo)["code"] == "object_not_found"


# ---------------------------------------------------------------------------
# 2. La porte MCP du Test -- rapport d'audit 09.
# ---------------------------------------------------------------------------


def test_the_run_collection_reads_the_same_read_model_as_the_console(a_holder, monkeypatch):
    import core.context_api as context_api
    import core.evaluation_runs as evaluation_runs

    seen: dict = {}

    def _list(conn, *, org_id, project_id, evidence_mode=None, limit=50):
        seen.update(
            {"org_id": org_id, "project_id": project_id, "mode": evidence_mode, "limit": limit}
        )
        return [{"id": "run_1", "lifecycle": "frozen", "evidence_mode": "offline"}]

    monkeypatch.setattr(context_api, "_project_org_id", lambda _c, _p: "org_x")
    monkeypatch.setattr(evaluation_runs, "list_evaluation_runs", _list)

    result = _tool("evaluation_mcp", "get_evaluation_runs")(
        project_id=PROJECT, evidence_mode="offline"
    )
    assert seen["org_id"] == "org_x"
    assert seen["project_id"] == PROJECT
    assert seen["mode"] == "offline"
    assert _payload(result)["count"] == 1


def test_naming_a_run_reads_its_overview_and_its_cases(a_holder, monkeypatch):
    """Les deux lecteurs de l'ecran, pas un troisieme resume compose ici."""
    import core.context_api as context_api
    import core.evaluation_runs as evaluation_runs

    monkeypatch.setattr(context_api, "_project_org_id", lambda _c, _p: "org_x")
    monkeypatch.setattr(
        evaluation_runs,
        "run_overview",
        lambda *a, **k: {
            "id": "run_1",
            "lifecycle": "frozen",
            "evidence_mode": "offline",
            "unresolved_pins": [],
            # Par dimension, par verdict -- jamais un taux.
            "verdict_counts": {"context_adherence": {"pass": 2, "unverifiable": 1}},
        },
    )
    monkeypatch.setattr(
        evaluation_runs, "run_cases", lambda *a, **k: {"run_id": "run_1", "cases": [{"id": "c1"}]}
    )

    result = _tool("evaluation_mcp", "get_evaluation_runs")(project_id=PROJECT, run_id="run_1")
    data = _payload(result)
    assert data["run"]["id"] == "run_1"
    assert len(data["cases"]) == 1
    text = result.content[0].text
    assert "pass=2" in text and "unverifiable=1" in text
    # Aucun taux, aucun score : les six dimensions ne se fusionnent pas.
    assert "%" not in text


def test_an_unknown_run_is_refused_by_name(a_holder, monkeypatch):
    import core.context_api as context_api
    import core.evaluation_runs as evaluation_runs

    def _raise(*_a, **_k):
        raise evaluation_runs.EvaluationNotFound("run_x")

    monkeypatch.setattr(context_api, "_project_org_id", lambda _c, _p: "org_x")
    monkeypatch.setattr(evaluation_runs, "run_overview", _raise)
    with pytest.raises(ToolError) as excinfo:
        _tool("evaluation_mcp", "get_evaluation_runs")(project_id=PROJECT, run_id="run_x")
    assert _refusal(excinfo)["code"] == "run_not_found"


def test_an_unknown_evidence_mode_is_refused_and_never_silently_dropped(a_holder):
    """Deux modes d'evidence ne se melangent pas ; un filtre inconnu ne s'ignore pas.

    Un `evidence_mode` avale sans un mot rendrait la collection ENTIERE sous le
    nom d'un filtre -- un `offline` demande, des cohortes observees servies.
    """
    with pytest.raises(ToolError) as excinfo:
        _tool("evaluation_mcp", "get_evaluation_runs")(
            project_id=PROJECT, evidence_mode="whatever"
        )
    assert _refusal(excinfo)["code"] == "invalid_param"


def test_adherence_reads_the_overview_that_had_no_reader(a_holder, monkeypatch):
    import core.adherence as adherence

    seen: dict = {}

    def _overview(conn, *, project_id, days):
        seen.update({"project_id": project_id, "days": days})
        return {
            "project_id": project_id,
            "observations": 4,
            "window": {"days": days},
            "by_basis": [
                {
                    "basis": "inferred_window",
                    "observations": 1,
                    "adherent": 1,
                    "not_adherent": 0,
                    "share_adherent": 1.0,
                    "means": "within a few minutes",
                },
                {
                    "basis": "observed_session",
                    "observations": 3,
                    "adherent": 2,
                    "not_adherent": 1,
                    "share_adherent": 0.6667,
                    "means": "known to be one exchange",
                },
            ],
        }

    monkeypatch.setattr(adherence, "adherence_overview", _overview)
    result = _tool("evaluation_mcp", "get_context_adherence")(project_id=PROJECT, days=7)
    assert seen == {"project_id": PROJECT, "days": 7}
    text = result.content[0].text
    # Le denominateur voyage avec la part : une part sur quatre observations se
    # lit sinon exactement comme une part sur quatre cents.
    assert "4 observation(s)" in text
    assert "2 of 3" in text and "1 of 1" in text
    # ET LES DEUX BASES NE SE MELANGENT PAS. `analyze-and-test.md:1330` : une
    # session tracee et une inference d'horloge « are reported apart and never
    # merged ». Cette porte imprimait la somme -- 3 adherents sur 4 -- juste avant
    # que l'ecran ne rende la meme ligne fusionnee.
    assert "3 of 4" not in text
    assert "0.75" not in text


def test_the_test_door_is_read_only_and_declares_itself_so():
    """AD-43 : la declaration EST le contrat, et elle ne ment pas sur l'effet."""
    for name in ("get_evaluation_runs", "get_context_adherence"):
        decl = _declaration("evaluation_mcp", name)
        assert decl["profile"] == "insights"
        assert decl["effect"] == "read"
        assert decl["confirmation_mode"] == "none"


def test_triggering_a_run_is_not_offered_by_this_door():
    """Ce qui n'est pas construit n'a pas de nom dans le catalogue.

    L'executeur existe (`core.evaluation_run_executor`) et ses appelants de
    production sont la console et le script operateur. Un outil qui pretendrait
    declencher un run serait un stub, et le document dit qu'un outil incapable
    de repondre doit le dire -- la forme honnete est de ne pas exister.
    """
    import core.evaluation_mcp as evaluation_mcp
    import core.mcp_profiles as profiles

    registered: list[str] = []

    def _record(_mcp, handler, **_kwargs):
        registered.append(handler.__name__)
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        evaluation_mcp.register(object())
    finally:
        profiles.register_profiled = original

    # `get_ai_path` is the reading door of the model's own path (2026-09-05,
    # mcp-tool-surface.md): a read, never a run.
    assert sorted(registered) == ["get_ai_path", "get_context_adherence", "get_evaluation_runs"]


# ---------------------------------------------------------------------------
# 3. L'autorite de total metric_grain -- rapport d'audit 03.
# ---------------------------------------------------------------------------


def _grain_stubs(monkeypatch, coverage: dict, *, concept=("sc_1", "revenue")):
    import core.metric_grain as metric_grain
    import core.metric_grain_api as metric_grain_api
    import core.semantic_model as semantic_model

    monkeypatch.setattr(
        metric_grain_api, "_concept", lambda conn, *, project_id, concept_id: concept
    )
    monkeypatch.setattr(semantic_model, "concept_names", lambda _c, _p: {"sc_1": "revenue"})
    seen: dict = {}

    def _coverage(conn, *, project_id, concept_id, concept_name, view_version_id=None):
        seen.update(
            {
                "project_id": project_id,
                "concept_id": concept_id,
                "concept_name": concept_name,
                "view_version_id": view_version_id,
            }
        )
        return coverage

    monkeypatch.setattr(metric_grain, "grain_coverage", _coverage)
    return seen


def test_the_grain_door_reads_the_shape_the_console_reads(a_holder, monkeypatch):
    """`grain_coverage` se decrivait deja comme << la forme que la console ET la
    porte MCP lisent >>. La console la lisait ; la porte n'existait pas."""
    coverage = {
        "concept_name": "revenue",
        "carriers": [
            {
                "datastream_id": "ds_a",
                "datastream_name": "A",
                "role": "total",
                "grain": ["date"],
            }
        ],
        "total_datastream_id": "ds_a",
        "next_gesture": None,
    }
    seen = _grain_stubs(monkeypatch, coverage)
    result = _tool("metric_grain_mcp", "get_metric_grain_authority")(
        project_id=PROJECT, concept_id="sc_1", semantic_view_version_id="svv_1"
    )
    assert seen["concept_id"] == "sc_1"
    assert seen["concept_name"] == "revenue"
    assert seen["view_version_id"] == "svv_1"
    assert _payload(result)["total_datastream_id"] == "ds_a"


def test_no_declared_total_reads_as_absent_and_names_the_gesture(a_holder, monkeypatch):
    """`null` est l'etat honnete, pas un defaut a deviner.

    C'est tout l'objet du refus du resolveur : avec plusieurs porteurs et aucune
    declaration, la requete est REFUSEE plutot que repondue en silence par l'un
    d'eux. L'outil doit rendre cette absence lisible, pas la combler.
    """
    coverage = {
        "concept_name": "revenue",
        "carriers": [
            {"datastream_id": "ds_a", "datastream_name": "A", "role": "breakdown", "grain": []},
            {"datastream_id": "ds_b", "datastream_name": "B", "role": "breakdown", "grain": []},
        ],
        "total_datastream_id": None,
        "next_gesture": "Declare which Datastream is authoritative for the total.",
    }
    _grain_stubs(monkeypatch, coverage)
    result = _tool("metric_grain_mcp", "get_metric_grain_authority")(
        project_id=PROJECT, concept_id="sc_1"
    )
    assert _payload(result)["total_datastream_id"] is None
    text = result.content[0].text
    assert "NO total authority declared" in text
    assert "Declare which Datastream is authoritative" in text


def test_a_measure_can_be_named_by_its_machine_name(a_holder, monkeypatch):
    seen = _grain_stubs(monkeypatch, {"concept_name": "revenue", "carriers": []})
    _tool("metric_grain_mcp", "get_metric_grain_authority")(
        project_id=PROJECT, concept_name="revenue"
    )
    assert seen["concept_id"] == "sc_1"


def test_a_measure_of_another_project_is_refused_like_the_console_refuses_it(
    a_holder, monkeypatch
):
    import core.metric_grain_api as metric_grain_api
    import core.semantic_model as semantic_model

    monkeypatch.setattr(
        metric_grain_api, "_concept", lambda conn, *, project_id, concept_id: None
    )
    monkeypatch.setattr(semantic_model, "concept_names", lambda _c, _p: {})
    with pytest.raises(ToolError) as excinfo:
        _tool("metric_grain_mcp", "get_metric_grain_authority")(
            project_id=PROJECT, concept_id="sc_elsewhere"
        )
    assert _refusal(excinfo)["code"] == "concept_not_found"


def test_naming_no_measure_at_all_is_refused(a_holder):
    with pytest.raises(ToolError) as excinfo:
        _tool("metric_grain_mcp", "get_metric_grain_authority")(project_id=PROJECT)
    assert _refusal(excinfo)["code"] == "missing_param"


def test_the_grain_door_declares_nothing_and_says_so():
    """La declaration reste le geste gouverne de la console.

    Deux autorites sur la meme phrase -- << d'ou viennent nos chiffres >> --
    seraient une de trop.
    """
    decl = _declaration("metric_grain_mcp", "get_metric_grain_authority")
    assert decl["effect"] == "read"
    assert decl["profile"] == "insights"


# ---------------------------------------------------------------------------
# 4. Le depot de remarque du Context Hub -- rapport d'audit 08.
# ---------------------------------------------------------------------------


def _remark_stubs(monkeypatch, node: dict | None):
    import core.context_api as context_api
    import core.context_review as context_review
    import core.context_store as context_store

    monkeypatch.setattr(context_api, "_project_org_id", lambda _c, _p: "org_x")
    monkeypatch.setattr(
        context_store, "get_procedure", lambda *a, **k: node
    )
    monkeypatch.setattr(context_store, "get_topic", lambda *a, **k: node)
    seen: dict = {}

    def _request(conn, **kwargs):
        seen.update(kwargs)
        return {
            "id": "crr_1",
            "node_type": kwargs["node_type"],
            "node_id": kwargs["node_id"],
            "node_version": kwargs["node_version"],
            "status": "open",
            "origin": kwargs["origin"],
            "requested_by": kwargs["requested_by"],
        }

    monkeypatch.setattr(context_review, "request_review", _request)
    return seen


def test_an_agent_deposits_a_remark_attributed_to_itself_and_marked_machine(
    a_holder, monkeypatch
):
    """L'asymetrie que l'audit a mesuree : l'agent LISAIT et ne pouvait pas deposer.

    `origin='agent'` n'est pas decoratif : une remarque de machine confondue avec
    une remarque humaine vaut moins que rien, et le producteur machine qui
    existait deja deposait sous une constante partagee faute d'identite. Ici il y
    en a une, donc la file enregistre QUEL agent l'a dite.
    """
    seen = _remark_stubs(
        monkeypatch, {"project_id": PROJECT, "version_number": 7, "id": "proc_x"}
    )
    result = _tool("context_remark_mcp", "add_context_remark")(
        project_id=PROJECT,
        node_type="procedure",
        node_id="proc_x",
        note="cette contrainte n'existe plus",
    )
    assert seen["origin"] == "agent"
    assert seen["requested_by"] == IDENTITY
    assert _payload(result)["status"] == "open"


def test_the_remark_is_filed_against_the_version_read_in_the_same_transaction(
    a_holder, monkeypatch
):
    """<< Cette contrainte n'existe plus >> ne veut rien dire sans la version.

    Elle est lue sur le NOEUD, jamais prise a l'appelant : une version fournie
    laisserait deposer une remarque contre une version que personne n'a servie.
    """
    seen = _remark_stubs(
        monkeypatch, {"project_id": PROJECT, "version_number": 7, "id": "proc_x"}
    )
    _tool("context_remark_mcp", "add_context_remark")(
        project_id=PROJECT, node_type="procedure", node_id="proc_x", note="stale"
    )
    assert seen["node_version"] == 7


def test_a_remark_on_a_platform_node_keeps_the_nodes_scope(a_holder, monkeypatch):
    """Une remarque sur une Skill de PLATEFORME concerne toute l'organisation.

    La regle est celle de la console, et `list_open` lit un `project_id` NULL
    exactement ainsi. La porte MCP ne peut pas en tenir une autre.
    """
    seen = _remark_stubs(
        monkeypatch, {"project_id": None, "version_number": 2, "id": "proc_platform"}
    )
    _tool("context_remark_mcp", "add_context_remark")(
        project_id=PROJECT, node_type="procedure", node_id="proc_platform", note="stale"
    )
    assert seen["project_id"] is None


def test_an_unknown_node_answers_the_same_thing_as_a_project_without_an_org(
    a_holder, monkeypatch
):
    """Deux refus indistinguables : comparer n'apprend pas qu'un noeud existe."""
    _remark_stubs(monkeypatch, None)
    with pytest.raises(ToolError) as excinfo:
        _tool("context_remark_mcp", "add_context_remark")(
            project_id=PROJECT, node_type="topic", node_id="top_x", note="stale"
        )
    assert _refusal(excinfo)["code"] == "node_not_found"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"node_type": "skill", "node_id": "x", "note": "n"}, "invalid_param"),
        ({"node_type": "topic", "node_id": "", "note": "n"}, "missing_param"),
        ({"node_type": "topic", "node_id": "x", "note": "   "}, "invalid_param"),
    ],
)
def test_a_remark_that_says_nothing_is_refused(a_holder, kwargs, code):
    """Une demande de relecture qui ne dit rien n'est pas une remarque, c'est un clic."""
    with pytest.raises(ToolError) as excinfo:
        _tool("context_remark_mcp", "add_context_remark")(project_id=PROJECT, **kwargs)
    assert _refusal(excinfo)["code"] == code


def test_the_remark_write_left_the_default_insights_catalogue():
    """AD-43 : un outil qui mute ne peut pas etre joignable sous `insights`.

    Et un `confirmed_write` exige une confirmation de confiance -- le middleware
    ne le liste ni ne l'appelle sans presence interactive attestee.
    """
    decl = _declaration("context_remark_mcp", "add_context_remark")
    assert decl["profile"] == "operations"
    assert decl["effect"] == "confirmed_write"
    assert decl["confirmation_mode"] in {"host", "human"}


def test_every_new_door_declares_a_profile_and_none_names_a_provider():
    """AD-42 / AD-43, sur les cinq portes de ce chantier a la fois."""
    doors = {
        "data_surface_mcp": ("get_data_surface",),
        "evaluation_mcp": ("get_evaluation_runs", "get_context_adherence"),
        "metric_grain_mcp": ("get_metric_grain_authority",),
        "context_remark_mcp": ("add_context_remark",),
    }
    from core.mcp_profiles import DATA_CLASSES, EFFECTS, PROFILES

    for module_name, names in doors.items():
        for name in names:
            decl = _declaration(module_name, name)
            assert decl["profile"] in PROFILES
            assert decl["effect"] in EFFECTS
            assert decl["data_class"] in DATA_CLASSES
            # Le nom d'un outil ne porte jamais un nom de fournisseur (AD-42).
            assert "_mcp" not in name


def test_no_new_door_returns_a_bare_payload_without_the_ad1_envelope(a_holder, monkeypatch):
    """Le canal texte porte un index, le detail voyage en structuredContent."""
    import core.data_surface as data_surface

    monkeypatch.setattr(
        data_surface,
        "compose_data_surface",
        lambda *a, **k: {"items": [{"name": "one", "states": {"evidence": "available"}}]},
    )
    result = _tool("data_surface_mcp", "get_data_surface")(project_id=PROJECT, lens="sources")
    assert result.structured_content["schema_version"] == "1"
    assert "meta" in result.structured_content
    assert isinstance(result.content[0].text, str)
    assert result.content[0].text.count("\n") < 15


def test_the_five_doors_are_the_ones_main_registers():
    """Un module ecrit et jamais branche est un outil que personne ne voit.

    La garde derivee de `tests/conformance` ne lit que `core/*.py` ; ce test-ci
    verifie l'autre moitie -- que `core.main` appelle bien chaque `register`.
    """
    import core.main as core_main  # noqa: F401 -- l'import EST ce qui enregistre
    from core.mcp_profiles import registered_declarations

    names = {declaration.name for declaration in registered_declarations()}
    for expected in (
        "get_data_surface",
        "get_evaluation_runs",
        "get_context_adherence",
        "get_metric_grain_authority",
        "add_context_remark",
    ):
        assert expected in names, f"{expected} n'est pas assemble par core.main"
