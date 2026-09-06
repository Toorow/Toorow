"""La porte MCP de DEFINITION du Test -- ce que ce Projet appelle digne de confiance.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST CELA. L'audit de gap du 2026-09-05 a
mesure le Test comme le lot le plus dense des dix gestes qu'une personne fait et
qu'un modele ne peut pas faire, et la ligne de celui-ci disait *« none. no tool
creates, versions or retires one »*. La porte est batie sur UNE promesse : **elle
compose par les MEMES fonctions que l'ecran** (`core.golden_questions`, le module
que `golden_questions_api.py` traduit depuis HTTP). Une promesse pareille ne se
lit pas dans le code : elle se verifie en remplacant le service par une doublure
et en exigeant que l'outil l'ait appele, avec les arguments que la console lui
passe.

Une SECONDE validation ici -- un `if` de forme sur la definition avant d'appeler
le service -- et l'agent et l'ecran finiraient par ne pas etre d'accord sur ce
qu'est une definition acceptable. C'est exactement le defaut que le service
unique existe pour retirer, et c'est ce que le premier test mesure.

AUCUN POSTGRES. Ce qui est sous test n'est pas la lecture ni l'ecriture -- elles
ont leurs propres tests, pg-gated, dans `test_golden_questions.py` -- mais le
CHAINAGE : garde, puis service partage, puis enveloppe AD-1. La garde de portee a
son harnais a elle (`tests/isolation/test_mcp_tool_scope_refusal.py`, ou les trois
outils entrent le jour ou ils sont ecrits) et n'est pas redite ici ; elle est
neutralisee pour que ces tests mesurent ce qui vient apres.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

IDENTITY = "person_tester"
PROJECT = "proj_EXAMPLE"
ORG = "org_EXAMPLE"


def _tool(tool_name: str):
    """Le handler que `golden_question_mcp` enregistre, capte a l'enregistrement.

    Meme patron que `test_mcp_doors_for_audited_surfaces._tool`. Ces outils sont
    des fonctions de module, mais passer par `register` prouve en plus qu'ils
    sont bien enregistres -- un outil ecrit et jamais declare serait vert ici
    autrement.
    """
    import core.golden_question_mcp as door
    import core.mcp_profiles as profiles

    captured: dict[str, object] = {}

    def _record(_mcp, handler, **_kwargs):
        captured[handler.__name__] = handler
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        door.register(object())
    finally:
        profiles.register_profiled = original
    return captured[tool_name]


def _declaration(tool_name: str) -> dict:
    """La declaration `register_profiled` d'un outil, telle qu'elle est posee."""
    import core.golden_question_mcp as door
    import core.mcp_profiles as profiles

    seen: dict[str, dict] = {}

    def _record(_mcp, handler, **kwargs):
        seen[handler.__name__] = kwargs
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        door.register(object())
    finally:
        profiles.register_profiled = original
    return seen[tool_name]


@pytest.fixture()
def a_reader(monkeypatch):
    """Une identite qui A le projet, et une connexion qui s'ouvre."""
    import core.db as core_db
    import core.golden_question_mcp as door
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)
    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(door, "_org_of", lambda _conn, _project: ORG)

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


@pytest.fixture()
def a_writer(monkeypatch):
    """Le meme, plus l'acces au rang `edit` resolu.

    Les deux ecritures ne passent PAS par le sceau de portee : elles passent par
    `resolve_strict_resource_access` au rang `edit`, comme `publish_shared_identity`.
    C'est cette resolution-la qui est neutralisee ici, et son refus est prouve
    ailleurs.
    """
    import core.db as core_db
    import core.mcp_scope as mcp_scope
    import core.project_access as project_access

    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *a, **k: SimpleNamespace(allowed=True, org_id=ORG),
    )

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
# 1. La lecture -- par les fonctions de l'ecran, jamais par une seconde requete.
# ---------------------------------------------------------------------------


def test_the_read_composes_through_the_screens_own_functions(a_reader, monkeypatch):
    """LA promesse : `list_golden_questions` du service, avec ses arguments."""
    import core.golden_questions as service

    seen: dict = {}

    def _list(conn, *, org_id, project_id, lifecycle=None):
        seen.update(
            {"conn": conn, "org_id": org_id, "project_id": project_id, "lifecycle": lifecycle}
        )
        return [
            {
                "id": "gq_1",
                "title": "Spend reconciles",
                "owner": "owner@example.com",
                "lifecycle": "active",
                "current_version": {"version_number": 2, "severity": "blocking"},
            }
        ]

    monkeypatch.setattr(service, "list_golden_questions", _list)
    result = _tool("list_golden_questions")(project_id=PROJECT, lifecycle="active")

    assert seen["org_id"] == ORG
    assert seen["project_id"] == PROJECT
    assert seen["lifecycle"] == "active"
    assert seen["conn"] is not None
    assert _payload(result)["total"] == 1
    assert "Spend reconciles" in result.content[0].text


def test_one_question_is_read_through_get_golden_question(a_reader, monkeypatch):
    """Nommer un id lit LA tete et son historique, jamais la collection filtree."""
    import core.golden_questions as service

    seen: dict = {}

    def _get(conn, *, org_id, project_id, golden_question_id):
        seen["golden_question_id"] = golden_question_id
        return {
            "id": golden_question_id,
            "title": "Spend reconciles",
            "owner": "owner@example.com",
            "lifecycle": "active",
            "versions": [{"id": "gqv_2"}, {"id": "gqv_1"}],
        }

    monkeypatch.setattr(service, "get_golden_question", _get)
    result = _tool("list_golden_questions")(project_id=PROJECT, golden_question_id="gq_1")

    assert seen["golden_question_id"] == "gq_1"
    assert _payload(result)["golden_question"]["title"] == "Spend reconciles"
    assert "2 version(s)" in result.content[0].text


def test_an_empty_collection_names_the_gesture_that_fills_it(a_reader, monkeypatch):
    """Une liste vide dit pourquoi et NOMME le geste -- jamais une table.

    `CLAUDE.md` : « Une liste vide dit pourquoi, et nomme le geste qui la
    remplit. Jamais un etat de deploiement, jamais un terme de la base. »
    """
    import core.golden_questions as service

    monkeypatch.setattr(service, "list_golden_questions", lambda *a, **k: [])
    text = _tool("list_golden_questions")(project_id=PROJECT).content[0].text

    assert "publish_golden_question_version" in text
    assert "app.golden_questions" not in text
    assert "migration" not in text.lower()


def test_the_options_are_served_by_the_validators_own_reader(a_reader, monkeypatch):
    """Les choix offerts viennent des tables que le validateur lit.

    Un catalogue tenu a la main continue d'offrir un domaine le jour ou il est
    archive ; ce qui est offert et ce qui est accepte ne peuvent pas deriver
    quand c'est la meme fonction.
    """
    import core.golden_questions as service

    called: dict = {}

    def _options(conn, *, org_id, project_id):
        called.update({"org_id": org_id, "project_id": project_id})
        return {
            "business_domains": [{"id": "bd_1", "latest_version_number": 3}],
            "semantic_views": [{"semantic_view_version_id": "svv_1"}],
        }

    monkeypatch.setattr(service, "golden_question_options", _options)
    monkeypatch.setattr(service, "list_golden_questions", lambda *a, **k: [])
    result = _tool("list_golden_questions")(project_id=PROJECT, options=True)

    assert called["org_id"] == ORG
    assert _payload(result)["options"]["business_domains"][0]["id"] == "bd_1"
    assert "1 governed Business Domain(s)" in result.content[0].text


# ---------------------------------------------------------------------------
# 2. L'ecriture -- valider d'abord, puis la fonction que le dialogue appelle.
# ---------------------------------------------------------------------------


def test_publishing_validates_before_it_writes_anything(a_writer, monkeypatch):
    """L'ordre EST le contrat : valider, puis creer. Jamais l'inverse."""
    import core.golden_questions as service

    order: list[str] = []

    def _validate(conn, *, org_id, project_id, payload):
        order.append("validate")
        assert payload == {"result_type": "scalar"}
        return SimpleNamespace(content_hash="sha256:abc")

    def _create(conn, *, org_id, project_id, title, owner, validated, actor):
        order.append("create")
        assert validated.content_hash == "sha256:abc"
        assert actor == IDENTITY
        return {
            "golden_question_id": "gq_new",
            "version_id": "gqv_1",
            "version_number": 1,
            "content_hash": validated.content_hash,
            "created_at": None,
        }

    monkeypatch.setattr(service, "validate_golden_question_version", _validate)
    monkeypatch.setattr(service, "create_golden_question", _create)
    result = _tool("publish_golden_question_version")(
        project_id=PROJECT,
        title="Spend reconciles",
        owner="owner@example.com",
        definition={"result_type": "scalar"},
    )

    assert order == ["validate", "create"]
    assert _payload(result)["created"] is True
    assert _payload(result)["golden_question_id"] == "gq_new"


def test_naming_a_head_publishes_its_next_version_and_creates_nothing(a_writer, monkeypatch):
    """Avec un id : la version suivante. La tete n'est jamais recreee."""
    import core.golden_questions as service

    def _boom(*a, **k):  # pragma: no cover -- il doit ne jamais etre appele
        raise AssertionError("create_golden_question must not be called with an id")

    monkeypatch.setattr(
        service,
        "validate_golden_question_version",
        lambda *a, **k: SimpleNamespace(content_hash="sha256:def"),
    )
    monkeypatch.setattr(service, "create_golden_question", _boom)
    monkeypatch.setattr(
        service,
        "create_golden_question_version",
        lambda conn, *, org_id, project_id, golden_question_id, validated, actor: {
            "golden_question_id": golden_question_id,
            "version_id": "gqv_3",
            "version_number": 3,
            "predecessor_version_id": "gqv_2",
            "content_hash": validated.content_hash,
            "created_at": None,
        },
    )
    result = _tool("publish_golden_question_version")(
        project_id=PROJECT, golden_question_id="gq_1", definition={"result_type": "scalar"}
    )

    assert _payload(result)["created"] is False
    assert _payload(result)["version_number"] == 3
    assert "immutable" in result.content[0].text


def test_a_refused_definition_travels_with_every_reason_and_its_subject(a_writer, monkeypatch):
    """Un modele devine plus fort qu'une personne : les raisons voyagent.

    La console repond 422 avec la liste structuree complete parce qu'« un
    appelant qui ne voit pas pourquoi il est refuse devinera ». Un code nu ici
    ferait deviner le modele exactement de la meme facon.
    """
    import core.golden_questions as service

    def _validate(*a, **k):
        raise service.GoldenQuestionRefused(
            "invalid_definition",
            "the definition is not acceptable",
            [
                service.Refusal("stale_domain_version", "domain version 2 is archived", "business_domain_version_number"),
                service.Refusal("missing_view_pin", "no Semantic View version pinned", "semantic_view_version_id"),
            ],
        )

    monkeypatch.setattr(service, "validate_golden_question_version", _validate)
    with pytest.raises(ToolError) as excinfo:
        _tool("publish_golden_question_version")(
            project_id=PROJECT, golden_question_id="gq_1", definition={"x": 1}
        )

    refusal = _refusal(excinfo)
    assert refusal["code"] == "invalid_definition"
    assert "business_domain_version_number: domain version 2 is archived" in refusal["message"]
    assert "semantic_view_version_id: no Semantic View version pinned" in refusal["message"]


# ---------------------------------------------------------------------------
# 3. Le cycle de vie -- les transitions declarees, et `archived` terminal.
# ---------------------------------------------------------------------------


def test_the_lifecycle_change_goes_through_the_services_own_transition_table(
    a_writer, monkeypatch
):
    """Aucune table de transitions ici. Le service en a une, et c'est la seule."""
    import core.golden_questions as service

    seen: dict = {}

    def _set(conn, *, org_id, project_id, golden_question_id, lifecycle, actor, owner=None):
        seen.update(
            {
                "golden_question_id": golden_question_id,
                "lifecycle": lifecycle,
                "actor": actor,
                "owner": owner,
            }
        )
        return {"golden_question_id": golden_question_id, "lifecycle": lifecycle, "owner": "owner@example.com"}

    monkeypatch.setattr(service, "set_lifecycle", _set)
    result = _tool("set_golden_question_lifecycle")(
        project_id=PROJECT, golden_question_id="gq_1", lifecycle="deprecated"
    )

    assert seen["lifecycle"] == "deprecated"
    assert seen["actor"] == IDENTITY
    assert seen["owner"] is None
    assert _payload(result)["lifecycle"] == "deprecated"
    assert "terminal" not in result.content[0].text


def test_archiving_says_in_words_that_it_cannot_be_undone(a_writer, monkeypatch):
    """`archived` est terminal. Le lecteur l'apprend AVANT d'en avoir besoin."""
    import core.golden_questions as service

    monkeypatch.setattr(
        service,
        "set_lifecycle",
        lambda *a, **k: {"golden_question_id": "gq_1", "lifecycle": "archived", "owner": "o"},
    )
    text = _tool("set_golden_question_lifecycle")(
        project_id=PROJECT, golden_question_id="gq_1", lifecycle="archived"
    ).content[0].text

    assert "terminal" in text
    assert "cannot return" in text


# ---------------------------------------------------------------------------
# 4. Les refus de forme -- ils parlent de l'argument, donc ils tombent d'abord.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "code"),
    [
        ("list_golden_questions", {"project_id": ""}, "missing_param"),
        ("list_golden_questions", {"project_id": PROJECT, "lifecycle": "retired"}, "invalid_param"),
        ("publish_golden_question_version", {"project_id": "", "definition": {"a": 1}}, "missing_param"),
        ("publish_golden_question_version", {"project_id": PROJECT, "definition": {}}, "missing_param"),
        # Ni id ni (titre + proprietaire) : le refus NOMME les deux chemins.
        ("publish_golden_question_version", {"project_id": PROJECT, "definition": {"a": 1}}, "missing_param"),
        ("set_golden_question_lifecycle", {"project_id": PROJECT, "golden_question_id": "", "lifecycle": "active"}, "missing_param"),
        ("set_golden_question_lifecycle", {"project_id": PROJECT, "golden_question_id": "gq_1", "lifecycle": "retired"}, "invalid_param"),
    ],
)
def test_the_door_refuses_the_argument_before_it_opens_anything(
    a_reader, monkeypatch, tool_name, kwargs, code
):
    """Un refus de forme ne dit rien du projet du voisin : il parle de l'appel."""
    import core.db as core_db

    def _never(_identity):  # pragma: no cover -- il doit ne jamais etre appele
        raise AssertionError("no connection may be opened for a malformed call")

    monkeypatch.setattr(core_db, "request_connection", _never)
    with pytest.raises(ToolError) as excinfo:
        _tool(tool_name)(**kwargs)
    assert _refusal(excinfo)["code"] == code


def test_a_new_question_without_title_and_owner_names_both_ways_out(a_reader):
    """Le message nomme les DEUX chemins : la tete neuve, ou l'id existant."""
    with pytest.raises(ToolError) as excinfo:
        _tool("publish_golden_question_version")(project_id=PROJECT, definition={"a": 1})
    message = _refusal(excinfo)["message"]
    assert "title and owner" in message
    assert "golden_question_id" in message


# ---------------------------------------------------------------------------
# 5. Les rangs declares -- ceux que le document ecrit, et pas d'autres.
# ---------------------------------------------------------------------------


def test_the_three_declarations_are_the_ranks_the_document_states():
    """`mcp-tool-surface.md`, amendement du 2026-09-05. Le rang EST le contrat.

    Une ecriture qui glisserait en `insights` entrerait dans le catalogue par
    defaut de tout hote et s'executerait sans ceremonie.
    """
    read = _declaration("list_golden_questions")
    assert (read["profile"], read["effect"], read["confirmation_mode"]) == (
        "insights",
        "read",
        "none",
    )
    for name in ("publish_golden_question_version", "set_golden_question_lifecycle"):
        write = _declaration(name)
        assert (write["profile"], write["effect"], write["confirmation_mode"]) == (
            "governance",
            "confirmed_write",
            "human",
        ), name
        assert write["data_class"] == "sensitive", name
