"""Story 38.18 AC1 -- l'operateur CHOISIT la version, au lieu de recevoir celle du jour.

L'AC1 dit : << The user selects an accessible retained `raw_import`, target
mapping/template versions and a governed scope >>. Le rejeu etait cable sur
`current_plan_version_id` / `current_mapping_version_id` : il ne repondait donc
qu'a une seule question -- << rejoue le mapping d'aujourd'hui >> -- ce qui ne
recupere rien quand le mapping d'aujourd'hui EST le probleme, c'est-a-dire dans
le cas que la story existe pour traiter.

Ces tests sont PURS : de faux curseurs, aucun DSN. Un test pg-gated skippe sans
base, et ce qui se prouve ici est un ordre de verifications et une forme de
proposition -- les deux se tiennent hors ligne.
"""

from __future__ import annotations

import pytest

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# EVERY STATEMENT THE EXERCISED PATHS ISSUE, NAMED ONCE. Declaration order is
# the order an `if/elif` would test in. The version LIST and the version PIN
# read the same relation, so each is separated by its own projection rather than
# by widening the shared `from` into a fragment that swallows both.
_STATEMENTS = StatementInventory(
    "test_inbound_reprocess_target._Cursor",
    # core/inbound_ingest.py:549 -- list_reprocess_target_versions.
    version_list=(
        "select v.id, v.version_number",
        "from app.datastream_mapping_versions v",
    ),
    # core/inbound_ingest.py:498 -- resolve_dispatch_bundle_for_target.
    version_lookup=(
        "select plan_version_id, executable",
        "from app.datastream_mapping_versions",
    ),
    # core/inbound_reprocess.py:275 -- _project_of.
    project_of="select project_id from app.datastreams",
    # core/inbound_reprocess.py:399 -- the pair in force, read when no target
    # was chosen, or when the chosen one was refused.
    current_pins="select current_plan_version_id, current_mapping_version_id",
    # core/inbound_ingest.py:622 -- _datastream_config, reached from
    # `prepare_reprocess` through `_template_facts` ->
    # `read_reprocess_template_version` on EVERY proposal. NEVER MODELLED: the
    # old chain had no tail, so this read fell through it and the `None` the
    # fake then returned was taken by the product for "this Datastream carries
    # no configuration".
    datastream_config="select config from app.datastreams",
)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317. `_Cursor` used to end its `elif` chain on nothing: a statement no
    branch recognised got `fetchone() -> None` and `fetchall() -> []`, which the
    product reads as << no row >> -- an ANSWER, not a refusal. A read added to
    these paths tomorrow would be measured as an absence, and this file would
    stay green while asserting a branch the product never took.
    """
    # The two statements that read the same relation really are separated.
    assert (
        _STATEMENTS.find(
            "SELECT v.id, v.version_number, v.executable, v.blocking_count, "
            "v.created_at, v.created_by, "
            "(v.id = d.current_mapping_version_id) AS is_current "
            "FROM app.datastream_mapping_versions v JOIN app.datastreams d "
            "ON d.id = v.datastream_id"
        )
        == "version_list"
    )
    assert (
        _STATEMENTS.find(
            "SELECT plan_version_id, executable "
            "FROM app.datastream_mapping_versions WHERE id = %s"
        )
        == "version_lookup"
    )

    with pytest.raises(UnknownStatement) as raised:
        # Plausible: it is the very next read of this path, issued as soon as a
        # Datastream config pins a Template. This fake pins none, so it must say
        # so out loud instead of answering "no Template row".
        _STATEMENTS.match(
            "SELECT template_code, version, is_active "
            "FROM app.file_source_templates WHERE id = %s AND project_id = %s"
        )
    # The statement that moved, and a neighbour to compare it against.
    assert "app.file_source_templates" in str(raised.value)
    assert "datastream_config" in str(raised.value)


class _Cursor:
    """A cursor that answers the STATEMENT -- and refuses everything else.

    AI-317. The `elif` chain used to end on nothing: a statement none of the
    branches recognised got `_one = None` and `_all = []`, which the product
    reads as "no row". `_datastream_config` (`inbound_ingest.py:622`) went
    through it on every proposal built here, and so would any read added to
    these paths tomorrow.
    """

    def __init__(self, rows: dict):
        self._rows = rows
        self._one = None
        self._all: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._one = None
        self._all = []
        self.description = None
        statement = _STATEMENTS.match(sql)
        if statement == "version_list":
            # The one projection `describe` refuses, and it says why: the last
            # column is `(v.id = d.current_mapping_version_id) AS is_current`,
            # an expression rather than a column. Named here, not guessed there.
            self.description = [
                ("id",),
                ("version_number",),
                ("executable",),
                ("blocking_count",),
                ("created_at",),
                ("created_by",),
                ("is_current",),
            ]
            self._all = self._rows.get("versions", [])
        elif statement == "version_lookup":
            self.description = describe(sql)
            self._one = self._rows.get("version_lookup")
        elif statement == "project_of":
            self.description = describe(sql)
            self._one = self._rows.get("project")
        elif statement == "current_pins":
            self.description = describe(sql)
            self._one = self._rows.get("current_pins")
        elif statement == "datastream_config":
            # `(config,)` -- one JSON column, the shape the product indexes. The
            # default is an EMPTY config, which is what a Datastream pinned to
            # no file-source Template really carries; the fake never invents a
            # Template pin, so no statement of that path is claimed above.
            self.description = describe(sql)
            self._one = self._rows.get("datastream_config", ({},))

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Conn:
    def __init__(self, rows: dict):
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows)


# ---------------------------------------------------------------------------
# La resolution d'une version choisie.
# ---------------------------------------------------------------------------


def test_a_chosen_version_resolves_its_own_plan_and_not_a_crossed_one():
    """La paire est ce qui a ete valide ENSEMBLE.

    Le plan n'est pas un second parametre : il est lu SUR la ligne du mapping
    choisi. Laisser un appelant croiser le mapping d'une version avec le plan
    d'une autre produirait une combinaison que personne n'a jamais relue.
    """
    from unittest.mock import patch

    from core.inbound_ingest import resolve_dispatch_bundle_for_target

    conn = _Conn({"version_lookup": ("plan_v7", True)})
    with patch(
        "core.inbound_ingest._resolve_server_dispatch_bundle",
        lambda conn, **kwargs: dict(kwargs),
    ):
        bundle = resolve_dispatch_bundle_for_target(
            conn,
            datastream_id="ds_1",
            project_id="proj_EXAMPLE",
            mapping_version_id="dmv_7",
        )
    assert bundle["plan_version_id"] == "plan_v7"
    assert bundle["mapping_version_id"] == "dmv_7"


def test_a_version_of_another_datastream_reads_as_absent():
    """Portee, et refus non enumerant.

    La recherche est bornee par (id, datastream_id, project_id) -- le composite
    exact que la table declare unique -- donc une version d'un Datastream voisin
    se lit comme ABSENTE plutot que comme une erreur de permission, qui
    confirmerait son existence.
    """
    from core.inbound_ingest import DatastreamNotIngestable, resolve_dispatch_bundle_for_target

    with pytest.raises(DatastreamNotIngestable) as exc:
        resolve_dispatch_bundle_for_target(
            _Conn({"version_lookup": None}),
            datastream_id="ds_1",
            project_id="proj_EXAMPLE",
            mapping_version_id="dmv_from_elsewhere",
        )
    assert "does not belong" in str(exc.value)


def test_a_non_executable_version_is_refused_here_and_not_downstream():
    """Un brouillon est refuse AU MOMENT DU CHOIX.

    Un rejeu peut deplacer le pointeur publie. Laisser passer une version non
    executable publierait des lignes sous un mapping jamais valide -- et le refus
    doit tomber ici, pas plus bas, ou il apparaitrait comme un echec d'analyse
    sans explication.
    """
    from core.inbound_ingest import DatastreamNotIngestable, resolve_dispatch_bundle_for_target

    with pytest.raises(DatastreamNotIngestable) as exc:
        resolve_dispatch_bundle_for_target(
            _Conn({"version_lookup": ("plan_v3", False)}),
            datastream_id="ds_1",
            project_id="proj_EXAMPLE",
            mapping_version_id="dmv_draft",
        )
    assert "not executable" in str(exc.value)


# ---------------------------------------------------------------------------
# La liste, sans laquelle le parametre n'est pas un choix.
# ---------------------------------------------------------------------------


def test_the_list_keeps_the_versions_that_cannot_be_replayed():
    """Un brouillon qu'on ne peut pas rejouer est exactement ce qu'on cherche
    quand on se demande pourquoi son candidat n'est pas propose.

    Le retirer de la liste transformerait un refus EXPLICABLE en absence, et une
    absence n'a pas de raison a lire.
    """
    import datetime

    from core.inbound_ingest import list_reprocess_target_versions

    now = datetime.datetime(2026, 8, 9, tzinfo=datetime.timezone.utc)
    conn = _Conn(
        {
            "versions": [
                ("dmv_9", 9, False, 3, now, "operator@example.com", False),
                ("dmv_8", 8, True, 0, now, "operator@example.com", True),
            ]
        }
    )
    versions = list_reprocess_target_versions(
        conn, datastream_id="ds_1", project_id="proj_EXAMPLE"
    )
    assert [v["version_number"] for v in versions] == [9, 8]
    assert versions[0]["executable"] is False
    assert versions[0]["blocking_count"] == 3
    assert versions[1]["is_current"] is True


# ---------------------------------------------------------------------------
# La proposition.
# ---------------------------------------------------------------------------


def _proposal(target=None, *, version_lookup=("plan_v7", True), available=True):
    from unittest.mock import patch

    from core.inbound_reprocess import ReprocessAvailability, prepare_reprocess

    conn = _Conn(
        {
            "project": ("proj_EXAMPLE",),
            "current_pins": ("plan_current", "dmv_current"),
            "version_lookup": version_lookup,
            "versions": [],
        }
    )
    with (
        patch(
            "core.inbound_reprocess.evaluate_reprocess",
            lambda *a, **k: ReprocessAvailability(
                raw_import_id="inbraw_1",
                datastream_id="ds_1",
                available=available,
                reason=None,
                detail=None,
            ),
        ),
        patch(
            "core.inbound_ingest._resolve_server_dispatch_bundle",
            lambda conn, **kwargs: dict(kwargs),
        ),
    ):
        return prepare_reprocess(
            conn,
            raw_import_id="inbraw_1",
            datastream_id="ds_1",
            target_mapping_version_id=target,
        )


def test_no_target_keeps_the_behaviour_every_caller_had():
    """L'absence de cible n'est pas une regression deguisee : c'est le defaut.

    Tous les appelants existants passent sans cible et doivent obtenir
    exactement ce qu'ils obtenaient -- la paire en vigueur.
    """
    proposal = _proposal()
    assert proposal["bound_versions"]["mapping_version_id"] == "dmv_current"
    assert proposal["bound_versions"]["plan_version_id"] == "plan_current"
    assert proposal["target_selected"] is False
    assert proposal["target_refused"] is None


def test_a_chosen_target_replaces_the_pair_and_says_so():
    """`target_selected` distingue << j'ai choisi celle-ci >> de << c'est celle
    en vigueur >>.

    Un meme identifiant ne separe pas les deux : la version courante EST un choix
    valide, et une personne qui la selectionne explicitement doit lire qu'elle
    l'a fait.
    """
    proposal = _proposal("dmv_7")
    assert proposal["bound_versions"]["mapping_version_id"] == "dmv_7"
    assert proposal["bound_versions"]["plan_version_id"] == "plan_v7"
    assert proposal["target_selected"] is True


def test_a_refused_target_is_reported_by_the_proposal_not_by_the_execution():
    """Une version refusee decouverte a l'execution serait un refus APRES
    confirmation -- c'est-a-dire apres qu'une personne a decide.

    Et la proposition cesse alors d'annoncer un effet : rien ne sera cree, et le
    pointeur publie ne bougera pas.
    """
    proposal = _proposal("dmv_draft", version_lookup=("plan_v3", False))
    assert proposal["target_refused"]
    assert "not executable" in proposal["target_refused"]
    assert proposal["target_selected"] is False
    assert proposal["creates_new_execution"] is False
    assert proposal["may_move_published_pointer"] is False


def test_the_proposal_carries_the_alternatives():
    """Un choix sans liste est un parametre, pas un choix.

    Sans elle, un operateur devrait deja connaitre un identifiant `dmv_` pour en
    nommer un, donc la seule version atteignable resterait la courante.
    """
    assert "available_versions" in _proposal()


# ---------------------------------------------------------------------------
# La trace.
# ---------------------------------------------------------------------------


def test_the_chosen_version_travels_in_the_audit_payload():
    """La version choisie est une PARTIE DE L'ACTE, pas un detail d'execution.

    Deux rejeux du meme fichier sous deux versions differentes sont deux actes
    differents, et une trace qui ne les distingue pas ne repond pas a la seule
    question qu'on lui posera : << pourquoi ces chiffres ont-ils change ? >>
    """
    import inspect

    from core import inbound_reprocess

    source = inspect.getsource(inbound_reprocess.execute_reprocess)
    payload_start = source.index("request_payload={")
    payload = source[payload_start : source.index("}", payload_start)]
    assert "target_mapping_version_id" in payload


def test_both_surfaces_take_the_target():
    """38.15 AC2 : console et MCP repondent la meme chose parce qu'ils appellent
    la meme commande. Un parametre present d'un cote seulement les ferait
    diverger sur la question la plus consequente de cette story."""
    import inspect

    from core import inbound_mcp, inbound_reprocess_api

    assert "target_mapping_version_id" in inspect.getsource(inbound_mcp)
    assert "target_mapping_version_id" in inspect.getsource(inbound_reprocess_api)


def test_the_original_bytes_are_never_rewritten():
    """La seconde moitie de l'AC1 : << original bytes and prior executions remain
    immutable >>.

    Le module lit l'objet en quarantaine et n'ecrit jamais dedans. Ce test le
    tient par la liste : y ajouter un ecrivain doit rougir ici.
    """
    import inspect

    from core import inbound_reprocess

    source = inspect.getsource(inbound_reprocess)
    for writer in (".put(", ".delete(", "UPDATE app.inbound_raw_imports"):
        assert writer not in source, writer
