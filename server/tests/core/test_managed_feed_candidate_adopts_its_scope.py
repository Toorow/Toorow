"""Le garde de l'EFFET : un import dans la portee d'un candidat n'en mint pas un second.

POURQUOI CE FICHIER EXISTE, et pourquoi les tests d'a cote ne suffisaient pas.

Le defaut mesure en preprod le 2026-08-08 : `materialize_draft_mutation` mint
l'execution du candidat, le pilote appelle `run_import` dans la portee
`raw_landing.candidate_execution`, et `open_import` demandait UNE SECONDE
execution. La garde de concurrence refusait a cause de la premiere -- la sienne.
Le candidat etait bloque par sa propre execution et le job mourait en
`managed_feed_import_in_progress`.

CE QUI COUVRAIT CE CHEMIN NE POUVAIT PAS LE VOIR :

* `test_managed_feed_candidate_imports_inside_the_execution_isolation` remplace
  `run_import` par un stub. Il prouve que la portee est OUVERTE avant l'import,
  et rien de ce qui se passe dedans -- or le blocage vit precisement dans le code
  que ce stub remplace ;
* les quatre tests de `test_csv_excel_import.py` qui nomment `open_import` le
  PATCHENT tous (`return_value=fake_result`). Son corps n'est donc exerce nulle
  part.

C'est le motif que `38-REPRISE.md` nomme : << un garde qui decrit un MOYEN reste
vert sur du code mort ; ecris le garde contre l'EFFET >>. L'effet ici n'est pas
<< la portee est ouverte >>, c'est << l'execution de la portee est ADOPTEE, et
aucune seconde n'est demandee >>.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

DATASTREAM = "ds_EXAMPLE"
PROJECT = "proj_EXAMPLE"
SCOPED_EXECUTION = "dse_SCOPED"
PLAN = "dsp_EXAMPLE"
MAPPING = "dmap_EXAMPLE"


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def _scoped_execution(**overrides: Any) -> dict[str, Any]:
    execution = {
        "id": SCOPED_EXECUTION,
        "datastream_id": DATASTREAM,
        "project_id": PROJECT,
        "plan_version_id": PLAN,
        "mapping_version_id": MAPPING,
        "state": "created",
    }
    execution.update(overrides)
    return execution


def _open(conn, *, execution: dict[str, Any] | None, in_scope: bool):
    """Drive the REAL `open_import`, with only its edges doubled."""
    from core import managed_feed_ledger as ledger
    from core.raw_landing import candidate_execution

    created: list[str] = []
    chosen: list[str] = []

    def _never(*_args, **_kwargs):
        created.append("create_execution")
        chosen.append("dse_MINTED")
        return {"id": "dse_MINTED", "datastream_id": DATASTREAM, "state": "created"}

    def _get_execution(execution_id, project_id, _conn_):
        from core.datastream_publication import ExecutionNotFound

        if execution is None or execution_id != SCOPED_EXECUTION:
            raise ExecutionNotFound()
        chosen.append(str(execution["id"]))
        return execution

    stack = [
        patch.object(ledger, "_fetch_ledger_by_key", return_value=None),
        patch.object(ledger, "_find_published_snapshot", return_value=None),
        patch("core.datastream_publication.create_execution", _never),
        patch("core.datastream_publication.get_execution", _get_execution),
        patch("core.audit.insert_audit_row", return_value=None),
        # `open_import` relit la ligne qu'il vient d'ecrire pour la rendre. Le
        # double la restitue avec l'execution effectivement retenue -- c'est CE
        # choix que ce fichier eprouve, pas le SQL de l'insertion.
        patch.object(
            ledger,
            "_fetch_ledger",
            side_effect=lambda _c, ledger_id, _p: {
                "id": ledger_id,
                "execution_id": chosen[-1] if chosen else None,
                "outcome": "opened",
            },
        ),
    ]
    import contextlib

    with contextlib.ExitStack() as es:
        for item in stack:
            es.enter_context(item)
        if in_scope:
            es.enter_context(candidate_execution(SCOPED_EXECUTION))
        result = ledger.open_import(
            datastream_id=DATASTREAM,
            project_id=PROJECT,
            plan_version_id=PLAN,
            mapping_version_id=MAPPING,
            feed_format="csv",
            projection_plan={},
            actor="owner@example.com",
            idempotency_key="qa-effect-guard",
            source_metadata={},
            content_hash=None,
            conn=conn,
        )
    return result, created


def test_an_import_inside_a_candidate_scope_adopts_it_instead_of_minting_a_second():
    """L'EFFET, pas le moyen : aucune seconde execution n'est demandee."""
    result, created = _open(_conn(), execution=_scoped_execution(), in_scope=True)

    assert created == [], (
        "une seconde execution a ete demandee dans la portee d'un candidat -- "
        "c'est exactement le blocage `managed_feed_import_in_progress`"
    )
    assert result["execution"]["id"] == SCOPED_EXECUTION
    # La ligne de registre est attachee a l'execution de la portee : c'est elle
    # qui atteste l'acte, et une autre attesterait un acte qui n'a pas eu lieu.
    assert result["ledger"]["execution_id"] == SCOPED_EXECUTION


def test_outside_any_scope_the_ordinary_import_still_mints_its_own():
    """La reparation est ADDITIVE. Un import ordinaire n'a pas de portee, et son
    comportement ne doit pas changer d'un octet -- sinon on repare un chemin en
    en cassant un autre, ce qui est le troc que ce depot passe son temps a
    defaire."""
    _result, created = _open(_conn(), execution=None, in_scope=False)

    assert created == ["create_execution"], (
        "hors portee, l'import doit continuer de minter son execution"
    )


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"datastream_id": "ds_AUTRE"}, "un autre flux"),
        ({"state": "published"}, "une execution terminale"),
        ({"plan_version_id": "dsp_AUTRE"}, "un autre plan"),
        ({"mapping_version_id": "dmap_AUTRE"}, "un autre mapping"),
    ],
)
def test_a_scope_that_does_not_designate_this_import_is_not_adopted(overrides, why):
    """Adopter n'importe quelle portee serait pire que le blocage.

    Attacher la ligne de registre a l'execution d'un AUTRE flux, ou a une
    execution terminale, ou a une execution minte contre d'autres versions,
    ferait attester un acte qui n'a pas eu lieu. La retombee sur
    `create_execution` est alors la reponse honnete -- et le 409 qu'elle leve est
    un VRAI conflit, pas l'auto-blocage que cette adoption existe pour retirer.
    """
    _result, created = _open(
        _conn(), execution=_scoped_execution(**overrides), in_scope=True
    )

    assert created == ["create_execution"], f"la portee designe {why} : elle ne s'adopte pas"


def test_this_guard_has_teeth():
    """Un garde qui reste vert sur le code d'AVANT ne prouve rien.

    La reparation etait deja landee (`681c314e`) quand ce fichier a ete ecrit,
    donc la faire rougir en revenant a HEAD etait impossible. La mutation la
    remplace : `_adopt_scoped_candidate` neutralise rend le comportement
    d'avant -- une seconde execution demandee dans la portee -- et le premier
    test DOIT alors echouer. S'il ne le fait pas, il ne mesure rien.
    """
    from core import managed_feed_ledger as ledger

    with patch.object(ledger, "_adopt_scoped_candidate", return_value=None):
        _result, created = _open(_conn(), execution=_scoped_execution(), in_scope=True)

    assert created == ["create_execution"], (
        "sans l'adoption, l'import DOIT redemander une execution -- si ce n'est "
        "pas le cas, le premier test de ce fichier passerait aussi sur le code "
        "casse, et ne garderait rien"
    )
