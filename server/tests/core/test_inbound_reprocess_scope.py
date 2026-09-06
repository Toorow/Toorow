"""Story 38.18 AC1 -- la PORTEE GOUVERNEE, et la version de Template qu'elle nomme.

L'AC1 dit, mot pour mot : << The user selects an accessible retained
`raw_import`, target mapping/template versions and a governed scope >>.

Deux morceaux manquaient. Le rejeu portait sur UN `raw_import`, donc il n'y avait
pas de portee du tout ; et la version de Template n'apparaissait nulle part, donc
une personne confirmait un rejeu sans savoir sous quel contrat il tournerait.

Le patron n'est pas invente ici. Il est ratifie :

    docs/product-architecture/datastream-workbench-and-wizard.md
      << Every destructive or durable repair follows `Prepare > Review exact
         scope and consequences > Confirm > Execute as a new durable
         operation`. >>              (mesure du 2026-08-10 : ligne 2191)
      << ... avec le compte AVANT l'acte (<< N of 46 columns stop landing >>) >>
                                     (mesure du 2026-08-10 : ligne 1080)

    Les DEUX numeros bougent : la meme phrase etait a `:2130` deux commits plus
    tot dans cette seule session. La phrase est la citation ; le numero n'est
    que l'endroit ou elle a ete trouvee ce jour-la.

Ce fichier tient quatre choses, et chacune par un CHANGEMENT de reponse entre
deux etats, jamais par la presence d'une cle :

  (A) Trois faits, trois mots. `not_applicable`, `unavailable`, `unknown`,
      `bound` -- jamais un `null` ni un `0` pour les quatre.
  (B) Le compte vient AVANT l'acte, avec les objets qu'il nomme, et un membre
      refuse est compte a part au lieu d'etre retire de la liste.
  (C) La borne est DITE. Une troncature muette est le defaut que
      `scan_truncated` existe pour empecher.
  (D) UNE operation durable porte la portee -- pas N operations muettes -- et le
      compte est CONTRAIGNANT : si la portee a bouge, rien n'est rejoue.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

_CSV = b"date,clicks\n2026-01-01,10\n"
_HASH = hashlib.sha256(_CSV).hexdigest()


# EVERY STATEMENT THE EXERCISED PATHS ISSUE, NAMED ONCE. Declaration order is the
# order an `if/elif` would test in, so the `sp_reprocess_template_list` savepoints
# come before the `sp_reprocess_template` ones they carry as a prefix. Each
# fragment is the shortest that separates its statement from the others; widening
# any of them would let two queries share one answer again.
_STATEMENTS = StatementInventory(
    "test_inbound_reprocess_scope._Cur",
    # core/inbound_reprocess.py:884 -- _scope_rows, the bounded candidate scan.
    scope_scan=(
        "select id, filename, content_hash",
        "from app.inbound_raw_imports where",
    ),
    # core/inbound_reprocess.py:1333 -- _origin_channel, read from the receipt.
    origin_channel=("select rx.channel", "join app.inbound_receipts"),
    # core/inbound_reprocess.py:275, :657, :1184 -- the Datastream's project.
    project_of="select project_id from app.datastreams",
    # core/inbound_reprocess.py:399, :1046 -- the pair in force.
    current_pins="select current_plan_version_id, current_mapping_version_id",
    # core/inbound_ingest.py:622 -- _datastream_config.
    datastream_config="select config from app.datastreams",
    # core/inbound_ingest.py:549 -- list_reprocess_target_versions.
    mapping_versions="from app.datastream_mapping_versions",
    # core/inbound_ingest.py:766 -- list_reprocess_template_versions.
    template_versions=("from app.file_source_templates", "order by version desc"),
    # core/inbound_ingest.py:667 -- the pinned Template, resolved.
    template_lookup=(
        "select template_code, version, is_active",
        "from app.file_source_templates",
    ),
    # core/inbound_ingest.py:763, :775, and :737 for the rollback -- the fence
    # around the version list. NEVER MODELLED BEFORE: the old chain had no tail,
    # so all six savepoint statements went through it in silence.
    template_list_savepoint_release="release savepoint sp_reprocess_template_list",
    template_list_savepoint_rollback=(
        "rollback to savepoint sp_reprocess_template_list"
    ),
    template_list_savepoint="savepoint sp_reprocess_template_list",
    # core/inbound_ingest.py:665, :673, and :737 -- the fence around the pin read.
    template_savepoint_release="release savepoint sp_reprocess_template",
    template_savepoint_rollback="rollback to savepoint sp_reprocess_template",
    template_savepoint="savepoint sp_reprocess_template",
)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317. `_Cur` used to end its `elif` chain with nothing: a statement no
    branch recognised got `fetchone() -> None` and `fetchall() -> []`, which the
    product reads as << no row >> -- an ANSWER, not a refusal. A read added to
    these paths tomorrow would be measured as an absence, and this file would
    stay green while asserting a branch the product never took.
    """
    assert (
        _STATEMENTS.find("SELECT project_id FROM app.datastreams WHERE id = %s")
        == "project_of"
    )
    # The prefix pair really is separated, and in the right order.
    assert (
        _STATEMENTS.find("SAVEPOINT sp_reprocess_template_list")
        == "template_list_savepoint"
    )
    assert _STATEMENTS.find("SAVEPOINT sp_reprocess_template") == "template_savepoint"

    with pytest.raises(UnknownStatement) as raised:
        _STATEMENTS.match(
            "SELECT legal_hold FROM app.inbound_raw_import_holds WHERE id = %s"
        )
    # The statement that moved, and a neighbour to compare it against.
    assert "app.inbound_raw_import_holds" in str(raised.value)
    assert "template_lookup" in str(raised.value)


class _MemoryStore:
    """A real store shape: it answers with bytes, and it can be made to fail."""

    def __init__(self, contents: dict[str, bytes]):
        self._contents = contents

    def get(self, uri):
        if uri not in self._contents:
            raise FileNotFoundError(uri)
        return self._contents[uri]


class _Cur:
    """A cursor that answers the QUERY, never its rank -- and REFUSES the rest.

    AI-317. It used to end in an `elif` chain with no tail: a statement none of
    the branches recognised got `_one = None` and `_all = []`, which the product
    reads as "no row" -- an answer, not a refusal. The savepoints that fence the
    two Template reads (`inbound_ingest.py:665/673/763/775`) went through it
    unnoticed, and so would any read added to these paths tomorrow.
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
        text = " ".join(str(sql).split())
        self._rows.setdefault("_sql", []).append(text)
        self._one = None
        self._all = []
        # Transaction control returns no result set, and psycopg says so with
        # `description = None`; every SELECT below sets it from the statement.
        self.description = None
        statement = _STATEMENTS.match(sql)
        if statement == "scope_scan":
            self.description = describe(sql)
            self._all = self._rows.get("scope_rows", [])
        elif statement == "origin_channel":
            self.description = describe(sql)
            self._one = self._rows.get("origin_channel", ("email",))
        elif statement == "project_of":
            self.description = describe(sql)
            self._one = self._rows.get("project", ("proj_EXAMPLE",))
        elif statement == "current_pins":
            self.description = describe(sql)
            self._one = self._rows.get("current_pins", ("plan_1", "dmv_1"))
        elif statement == "datastream_config":
            self.description = describe(sql)
            config = self._rows.get("config", {})
            self._one = None if config is None else (config,)
        elif statement == "mapping_versions":
            # The one projection `describe` refuses, and it says why: it carries
            # `(v.id = d.current_mapping_version_id) AS is_current`, which is an
            # expression, not a column. Named here rather than guessed there.
            self.description = [
                ("id",), ("version_number",), ("executable",), ("blocking_count",),
                ("created_at",), ("created_by",), ("is_current",),
            ]
            self._all = self._rows.get("mapping_version_rows", [])
        elif statement == "template_versions":
            self.description = describe(sql)
            self._all = self._rows.get("template_rows", [])
        elif statement == "template_lookup":
            self.description = describe(sql)
            self._one = self._rows.get("template_lookup")
        # Everything else in the inventory is a SAVEPOINT / RELEASE / ROLLBACK
        # TO: no rows, no description. Anything NOT in it raised above.

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Conn:
    def __init__(self, rows: dict):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def commit(self):
        self._rows["committed"] = True


def _raw(raw_import_id: str, *, uri: str, content_hash: str = _HASH, **over):
    row = {
        "raw_import_id": raw_import_id,
        "receipt_id": "inbrx_1",
        "datastream_id": "ds_1",
        "ordinal": 0,
        "filename": f"{raw_import_id}.csv",
        "media_type_declared": "text/csv",
        "size_bytes": len(_CSV),
        "content_hash": content_hash,
        "quarantine_uri": uri,
        "state": "LANDED",
        "legal_hold": False,
        "retention_expires_at": None,
    }
    row.update(over)
    return row


def _patch_raws(monkeypatch, rows: dict[str, dict]):
    import core.inbound_raw_imports as iri

    monkeypatch.setattr(
        iri,
        "get_raw_import",
        lambda conn, *, raw_import_id, datastream_id: rows.get(raw_import_id),
    )


# ===========================================================================
# (A) LA VERSION DE TEMPLATE -- trois faits, trois mots.
# ===========================================================================


def _template_state(rows: dict) -> dict:
    from core.inbound_ingest import read_reprocess_template_version

    return read_reprocess_template_version(
        _Conn(rows), datastream_id="ds_1", project_id="proj_EXAMPLE"
    )


def test_no_template_pin_is_not_applicable_and_not_a_null():
    """<< Ce Datastream n'epingle aucun Template >> est un FAIT, pas une absence.

    Un `null` ici se lirait << je n'ai pas trouve >>, ce qui enverrait quelqu'un
    reparer une liaison qui n'a jamais eu a exister.
    """
    state = _template_state({"config": {"source_owner": {}}})
    assert state["state"] == "not_applicable"
    assert state["reason"]
    assert "template_id" not in state


def test_a_pinned_template_that_resolves_says_bound_with_its_number():
    state = _template_state(
        {
            "config": {"source_owner": {"managed_feed_template_ref": "fst_A"}},
            "template_lookup": ("media_plan", 4, True),
        }
    )
    assert state["state"] == "bound"
    assert state["template_id"] == "fst_A"
    assert state["template_code"] == "media_plan"
    assert state["version"] == 4


def test_a_pin_that_resolves_to_no_row_is_unknown_and_not_not_applicable():
    """LE CHANGEMENT DE REPONSE EST LA PREUVE. Meme config, une seule difference
    -- le registre ne rend pas de ligne -- et le mot doit changer.

    Les confondre ferait lire << pas de Template >> a une liaison CASSEE, donc
    personne n'irait la reparer.
    """
    rows = {
        "config": {"source_owner": {"managed_feed_template_ref": "fst_A"}},
        "template_lookup": ("media_plan", 4, True),
    }
    assert _template_state(dict(rows))["state"] == "bound"

    rows["template_lookup"] = None
    broken = _template_state(rows)
    assert broken["state"] == "unknown"
    assert broken["state"] != "not_applicable"
    # La reference PERDUE est nommee : sans elle, il n'y a rien a chercher.
    assert broken["template_id"] == "fst_A"


def test_an_unreadable_registry_is_unavailable_and_not_unknown():
    """<< Le magasin n'a pas repondu >> n'est pas << la reference ne resout rien >>.

    Une panne de lecture rendue comme une reference cassee ferait recreer un
    Template qui existe.
    """
    from core.inbound_ingest import read_reprocess_template_version

    class _Boom(_Conn):
        def cursor(self):
            cur = _Cur(self._rows)
            original = cur.execute

            def execute(sql, params=None):
                if "FROM app.file_source_templates" in str(sql):
                    raise RuntimeError("connection reset")
                return original(sql, params)

            cur.execute = execute
            return cur

    state = read_reprocess_template_version(
        _Boom({"config": {"source_owner": {"managed_feed_template_ref": "fst_A"}}}),
        datastream_id="ds_1",
        project_id="proj_EXAMPLE",
    )
    assert state["state"] == "unavailable"
    assert state["template_id"] == "fst_A"


def test_an_unreadable_datastream_config_is_unavailable_not_not_applicable():
    from core.inbound_ingest import read_reprocess_template_version

    class _Boom(_Conn):
        def cursor(self):
            cur = _Cur(self._rows)

            def execute(sql, params=None):
                raise RuntimeError("connection reset")

            cur.execute = execute
            return cur

    state = read_reprocess_template_version(
        _Boom({}), datastream_id="ds_1", project_id="proj_EXAMPLE"
    )
    assert state["state"] == "unavailable"


def test_a_catalog_pinned_datastream_is_bound_and_not_not_applicable(monkeypatch):
    """La SECONDE realisation ratifiee. `file-source-ingestion.md` lie le Template
    par deux chemins -- << a reused catalog or client-saved Template >>.

    Un Datastream cree par le chemin catalogue epingle
    `{template_code, template_version}` sur son config. Le rendre
    `not_applicable` dirait qu'il n'a pas de Template alors qu'il en a un.
    """
    import core.import_templates as it

    monkeypatch.setattr(
        it, "get_template", lambda conn, code, version: {"contract": {"x": 1}}
    )
    state = _template_state(
        {"config": {"template_code": "media_plan", "template_version": 2}}
    )
    assert state["state"] == "bound"
    assert state["kind"] == "catalog"
    assert state["version"] == 2


def test_the_other_template_versions_are_listed_with_an_explainable_refusal():
    """Un choix sans liste est un parametre ; une liste sans raison est un mur.

    Les autres versions RESTENT affichees, marquees non rejouables, avec la
    phrase qui dit ou se fait le geste. Les retirer transformerait un refus
    explicable en absence -- exactement ce que `list_reprocess_target_versions`
    refuse deja pour les mappings.
    """
    import datetime

    from core.inbound_ingest import list_reprocess_template_versions

    now = datetime.datetime(2026, 8, 10, tzinfo=datetime.timezone.utc)
    rows = {
        "template_rows": [
            ("fst_B", 5, True, now, "operator@example.com"),
            ("fst_A", 4, True, now, "operator@example.com"),
        ]
    }
    listed = list_reprocess_template_versions(
        _Conn(rows),
        project_id="proj_EXAMPLE",
        template_code="media_plan",
        pinned_template_id="fst_A",
    )
    assert [entry["version"] for entry in listed] == [5, 4]
    assert listed[0]["replayable"] is False
    assert listed[0]["not_replayable_reason"]
    assert listed[1]["is_pinned"] is True
    assert listed[1]["replayable"] is True
    assert listed[1]["not_replayable_reason"] is None


def test_the_proposal_carries_the_template_version_and_its_alternatives(monkeypatch):
    """AC1 demande DEUX versions cibles. La proposition en portait une."""
    from core.inbound_reprocess import prepare_reprocess

    _patch_raws(monkeypatch, {"inbraw_1": _raw("inbraw_1", uri="memory://a")})
    proposal = prepare_reprocess(
        _Conn(
            {
                "config": {"source_owner": {"managed_feed_template_ref": "fst_A"}},
                "template_lookup": ("media_plan", 4, True),
                "template_rows": [],
            }
        ),
        raw_import_id="inbraw_1",
        datastream_id="ds_1",
        store=_MemoryStore({"memory://a": _CSV}),
    )
    assert proposal["template_version"]["state"] == "bound"
    assert proposal["template_version"]["version"] == 4
    assert "available_template_versions" in proposal


def test_the_template_version_travels_with_the_act(monkeypatch, tmp_path):
    """Deux rejeux du meme fichier sous deux epingles differentes sont deux actes
    differents. Une trace qui ne les distingue pas ne repond pas a
    << pourquoi ces chiffres ont-ils change ? >>.

    Le CHANGEMENT est la preuve : la meme execution sous deux Templates doit
    ecrire deux payloads differents.
    """
    from core.inbound_reprocess import execute_reprocess

    seen = []
    for version in (4, 5):
        conn, captured = _wire_execution(
            monkeypatch,
            rows={
                "config": {"source_owner": {"managed_feed_template_ref": "fst_A"}},
                "template_lookup": ("media_plan", version, True),
            },
            raws={"inbraw_1": _raw("inbraw_1", uri="memory://a")},
        )
        execute_reprocess(
            conn,
            raw_import_id="inbraw_1",
            datastream_id="ds_1",
            actor="operator",
            idempotency_key=f"ik-{version}",
            reason="mapping repaired",
            store=_MemoryStore({"memory://a": _CSV}),
        )
        seen.append(captured["specs"][0].request_payload["template_version"])
    assert seen[0]["version"] == 4
    assert seen[1]["version"] == 5
    assert seen[0] != seen[1]


# ===========================================================================
# La plomberie commune des tests d'execution.
# ===========================================================================


def _wire_execution(
    monkeypatch,
    *,
    rows: dict,
    raws: dict,
    ingest_results: dict | None = None,
):
    """Wire the operation seam and the pipeline so the mutation really runs.

    `execute_operation` is COUNTED, because "one durable operation carries the
    scope" is a claim about how many times it is called, and nothing else can
    prove it.
    """
    import core.inbound_ingest as ii
    import core.inbound_raw_imports as iri
    import core.inbound_scan as isc
    from core import operations
    from core.inbound_scan import ScanVerdict

    captured: dict = {"specs": [], "ingest_calls": []}

    def execute(operation_conn, spec, *, mutation):
        captured["specs"].append(spec)
        changed = mutation(operation_conn, "op-1")
        captured["mutation_result"] = changed.result
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(operations, "execute_operation", execute)
    monkeypatch.setattr(iri, "_resolve_org_id", lambda conn, **kw: "org-1")
    _patch_raws(monkeypatch, raws)

    def _ingest(conn, **kwargs):
        captured["ingest_calls"].append(kwargs)
        outcome = (ingest_results or {}).get(kwargs["raw_import_id"])
        return outcome or {"blocked": False, "ledger": {"id": "mfl_" + kwargs["raw_import_id"]}}

    monkeypatch.setattr(ii, "ingest_inbound_file", _ingest)
    monkeypatch.setattr(
        ii,
        "resolve_dispatch_bundle_for_acceptance",
        lambda conn, **kw: {"schema": "managed-file-dispatch-bundle-v1"},
    )
    monkeypatch.setattr(
        isc,
        "scan_bytes",
        lambda data, **kw: ScanVerdict(
            accepted=True,
            reason=None,
            detected_type="text/csv",
            declared_type="text/csv",
            size_bytes=len(data),
        ),
    )
    return _Conn(rows), captured


# ===========================================================================
# (B) LE COMPTE AVANT L'ACTE, avec les objets qu'il nomme.
# ===========================================================================


def _scope_row(raw_import_id: str, state: str = "LANDED"):
    import datetime

    return (
        raw_import_id,
        f"{raw_import_id}.csv",
        _HASH,
        len(_CSV),
        state,
        datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
    )


def test_the_scope_counts_before_the_act_and_names_its_objects(monkeypatch):
    """<< N of 46 columns stop landing >> est le patron ratifie : le compte, ET
    les objets. Un compte nu laisserait une personne confirmer << 3 livraisons >>
    sans pouvoir dire lesquelles.
    """
    from core.inbound_reprocess import prepare_reprocess_scope

    _patch_raws(
        monkeypatch,
        {
            "inbraw_1": _raw("inbraw_1", uri="memory://a"),
            "inbraw_2": _raw("inbraw_2", uri="memory://b"),
        },
    )
    proposal = prepare_reprocess_scope(
        _Conn(
            {
                "scope_rows": [_scope_row("inbraw_1"), _scope_row("inbraw_2")],
                "config": {"source_owner": {}},
            }
        ),
        datastream_id="ds_1",
        raw_import_ids=["inbraw_1", "inbraw_2"],
        store=_MemoryStore({"memory://a": _CSV, "memory://b": _CSV}),
    )
    assert proposal["scope"] == {
        "state": "counted",
        "examined": 2,
        "reprocessable": 2,
        "refused": 0,
    }
    assert [m["raw_import_id"] for m in proposal["members"]] == [
        "inbraw_1",
        "inbraw_2",
    ]
    assert proposal["members"][0]["filename"] == "inbraw_1.csv"
    assert proposal["selection"]["confirm_raw_import_ids"] == [
        "inbraw_1",
        "inbraw_2",
    ]
    assert proposal["creates_new_execution"] is True


def test_a_refused_member_is_counted_apart_and_never_dropped(monkeypatch):
    """LE CHANGEMENT DE REPONSE. Meme demande, un objet illisible en plus : le
    compte des rejouables BAISSE et le refus reste dans la liste, avec sa raison.

    Le retirer donnerait un compte juste et une liste fausse -- une personne
    croirait que le fichier n'a jamais ete demande.
    """
    from core.inbound_reprocess import UNAVAILABLE_NO_OBJECT, prepare_reprocess_scope

    _patch_raws(
        monkeypatch,
        {
            "inbraw_1": _raw("inbraw_1", uri="memory://a"),
            "inbraw_2": _raw("inbraw_2", uri="memory://gone"),
        },
    )
    proposal = prepare_reprocess_scope(
        _Conn(
            {
                "scope_rows": [_scope_row("inbraw_1"), _scope_row("inbraw_2")],
                "config": {"source_owner": {}},
            }
        ),
        datastream_id="ds_1",
        raw_import_ids=["inbraw_1", "inbraw_2"],
        store=_MemoryStore({"memory://a": _CSV}),
    )
    assert proposal["scope"]["examined"] == 2
    assert proposal["scope"]["reprocessable"] == 1
    assert proposal["scope"]["refused"] == 1
    refused = [m for m in proposal["members"] if not m["available"]]
    assert len(refused) == 1
    assert refused[0]["reason"] == UNAVAILABLE_NO_OBJECT
    # Et il ne part PAS a la confirmation.
    assert proposal["selection"]["confirm_raw_import_ids"] == ["inbraw_1"]


def test_a_foreign_identifier_reads_as_not_found_never_as_a_refusal(monkeypatch):
    """Un refus ne revele jamais l'existence. Un id d'un Datastream voisin ne
    correspond a aucune ligne dans la portee, et rend `raw_import_not_found` --
    la meme reponse qu'un id qui n'existe pas.

    Et il n'est pas SILENCIEUSEMENT retire : une portee qui avale les ids
    inconnus laisserait croire qu'ils ont ete rejoues.
    """
    from core.inbound_reprocess import UNAVAILABLE_NOT_FOUND, prepare_reprocess_scope

    _patch_raws(monkeypatch, {"inbraw_1": _raw("inbraw_1", uri="memory://a")})
    proposal = prepare_reprocess_scope(
        _Conn({"scope_rows": [_scope_row("inbraw_1")], "config": {"source_owner": {}}}),
        datastream_id="ds_1",
        raw_import_ids=["inbraw_1", "inbraw_from_elsewhere"],
        store=_MemoryStore({"memory://a": _CSV}),
    )
    stranger = [
        m for m in proposal["members"] if m["raw_import_id"] == "inbraw_from_elsewhere"
    ]
    assert len(stranger) == 1
    assert stranger[0]["reason"] == UNAVAILABLE_NOT_FOUND
    assert proposal["scope"]["refused"] == 1


# ===========================================================================
# (C) LA BORNE EST DITE.
# ===========================================================================


def test_the_bound_is_declared_and_a_truncated_scan_says_so(monkeypatch):
    """LE CHANGEMENT DE REPONSE, sur la seule chose qui bouge : une ligne de plus
    que la borne.

    A la borne exacte, `scan_truncated` est FAUX -- annoncer une troncature qui
    n'a pas eu lieu est son propre petit mensonge. Une ligne au-dela, il est
    VRAI, et la liste s'arrete a la borne.
    """
    from core.inbound_reprocess import prepare_reprocess_scope

    _patch_raws(
        monkeypatch,
        {f"inbraw_{i}": _raw(f"inbraw_{i}", uri="memory://a") for i in range(5)},
    )
    store = _MemoryStore({"memory://a": _CSV})

    exact = prepare_reprocess_scope(
        _Conn(
            {
                "scope_rows": [_scope_row(f"inbraw_{i}") for i in range(2)],
                "config": {"source_owner": {}},
            }
        ),
        datastream_id="ds_1",
        criterion={"states": ["LANDED"]},
        limit=2,
        store=store,
    )
    assert exact["scan_truncated"] is False
    assert exact["scan_limit"] == 2
    assert exact["scope"]["examined"] == 2

    over = prepare_reprocess_scope(
        _Conn(
            {
                "scope_rows": [_scope_row(f"inbraw_{i}") for i in range(3)],
                "config": {"source_owner": {}},
            }
        ),
        datastream_id="ds_1",
        criterion={"states": ["LANDED"]},
        limit=2,
        store=store,
    )
    assert over["scan_truncated"] is True
    assert over["scope"]["examined"] == 2


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({}, "either raw_import_ids or a bounded criterion"),
        (
            {"raw_import_ids": ["a"], "criterion": {"states": ["LANDED"]}},
            "not both",
        ),
        ({"raw_import_ids": ["a", "a"]}, "duplicates"),
        ({"criterion": {"nope": 1}}, "unsupported criterion keys"),
        ({"criterion": {"states": ["NOPE"]}}, "unknown raw-import states"),
        ({"criterion": {"received_from": "yesterday"}}, "ISO-8601"),
        ({"criterion": {}}, "either raw_import_ids or a bounded criterion"),
    ],
)
def test_a_malformed_scope_is_refused_before_any_read(kwargs, fragment):
    """Une cle de critere non reconnue et ignoree en silence ELARGIRAIT la portee
    sans le dire, ce qui est la seule chose qu'une portee gouvernee ne peut pas
    faire.
    """
    from core.inbound_reprocess import ReprocessValidationError, prepare_reprocess_scope

    with pytest.raises(ReprocessValidationError, match=fragment):
        prepare_reprocess_scope(_Conn({}), datastream_id="ds_1", **kwargs)


def test_a_scope_larger_than_the_bound_is_refused_by_name():
    from core.inbound_reprocess import (
        MAX_SCOPE_MEMBERS,
        ReprocessValidationError,
        prepare_reprocess_scope,
    )

    with pytest.raises(ReprocessValidationError, match="bounded at"):
        prepare_reprocess_scope(
            _Conn({}),
            datastream_id="ds_1",
            raw_import_ids=[f"inbraw_{i}" for i in range(MAX_SCOPE_MEMBERS + 1)],
        )


# ===========================================================================
# (D) UNE operation durable, et un compte CONTRAIGNANT.
# ===========================================================================


def test_one_durable_operation_carries_the_whole_scope(monkeypatch):
    """N operations muettes rendraient << qu'a fait cette reparation ? >> sans
    reponse, et un renvoi rejouerait certains membres et pas d'autres.

    La preuve est un COMPTE : `execute_operation` une fois, `ingest_inbound_file`
    trois fois.
    """
    from core.inbound_reprocess import execute_reprocess_scope

    ids = ["inbraw_1", "inbraw_2", "inbraw_3"]
    conn, captured = _wire_execution(
        monkeypatch,
        rows={"config": {"source_owner": {}}},
        raws={i: _raw(i, uri=f"memory://{i}") for i in ids},
    )
    out = execute_reprocess_scope(
        conn,
        datastream_id="ds_1",
        raw_import_ids=ids,
        actor="operator",
        idempotency_key="ik-scope",
        reason="mapping repaired for the whole month",
        store=_MemoryStore({f"memory://{i}": _CSV for i in ids}),
    )

    assert len(captured["specs"]) == 1
    assert len(captured["ingest_calls"]) == 3
    assert captured["specs"][0].command_type == "inbound.raw_import.scope_reprocessed"
    # La portee est NOMMEE dans la trace, pas resumee par un compte.
    assert captured["specs"][0].request_payload["raw_import_ids"] == sorted(ids)
    assert captured["specs"][0].request_payload["scope_size"] == 3
    assert out["scope"] == {
        "state": "counted",
        "confirmed": 3,
        "landed": 3,
        "refused": 0,
    }
    assert out["status"] == "landed"
    assert out["operation_id"] == "op-1"


def test_each_member_gets_its_own_message_id(monkeypatch):
    """Deux membres d'une meme portee qui partageraient un `message_id` feraient
    rejouer le second comme un doublon du premier : `run_import` est idempotent
    dessus, donc la collision serait un no-op qui ressemble a un succes.
    """
    from core.inbound_reprocess import execute_reprocess_scope

    ids = ["inbraw_1", "inbraw_2"]
    conn, captured = _wire_execution(
        monkeypatch,
        rows={"config": {"source_owner": {}}},
        raws={i: _raw(i, uri=f"memory://{i}") for i in ids},
    )
    execute_reprocess_scope(
        conn,
        datastream_id="ds_1",
        raw_import_ids=ids,
        actor="operator",
        idempotency_key="ik-scope",
        reason="mapping repaired",
        store=_MemoryStore({f"memory://{i}": _CSV for i in ids}),
    )
    messages = [call["message_id"] for call in captured["ingest_calls"]]
    assert len(set(messages)) == 2
    # Et la derivation est celle du rejeu unitaire : meme cle, meme suffixe.
    suffix = hashlib.sha256(b"ik-scope").hexdigest()[:16]
    assert all(m.endswith(suffix) for m in messages)
    assert messages[0].startswith("reprocess:inbraw_1:")


def test_a_scope_that_moved_replays_nothing(monkeypatch):
    """LE COMPTE EST CONTRAIGNANT, sinon il n'est qu'une decoration.

    LE CHANGEMENT DE REPONSE : les memes trois ids, la meme demande. Avec les
    trois objets lisibles, trois rejeux. Avec un objet disparu entre le compte et
    l'acte, ZERO -- pas deux. Rejouer 2 sur 3 rendrait faux le nombre que la
    personne a lu, sans qu'elle ait moyen de l'apprendre.
    """
    from core.inbound_reprocess import (
        UNAVAILABLE_SCOPE_MOVED,
        ReprocessUnavailable,
        execute_reprocess_scope,
    )

    ids = ["inbraw_1", "inbraw_2", "inbraw_3"]
    raws = {i: _raw(i, uri=f"memory://{i}") for i in ids}

    conn, captured = _wire_execution(
        monkeypatch, rows={"config": {"source_owner": {}}}, raws=raws
    )
    execute_reprocess_scope(
        conn,
        datastream_id="ds_1",
        raw_import_ids=ids,
        actor="operator",
        idempotency_key="ik-1",
        reason="all three",
        store=_MemoryStore({f"memory://{i}": _CSV for i in ids}),
    )
    assert len(captured["ingest_calls"]) == 3

    conn, captured = _wire_execution(
        monkeypatch, rows={"config": {"source_owner": {}}}, raws=raws
    )
    with pytest.raises(ReprocessUnavailable) as exc:
        execute_reprocess_scope(
            conn,
            datastream_id="ds_1",
            raw_import_ids=ids,
            actor="operator",
            idempotency_key="ik-2",
            reason="all three",
            # inbraw_2 a disparu du magasin depuis la preparation.
            store=_MemoryStore(
                {"memory://inbraw_1": _CSV, "memory://inbraw_3": _CSV}
            ),
        )
    assert exc.value.code == UNAVAILABLE_SCOPE_MOVED
    assert captured["ingest_calls"] == []
    assert captured["specs"] == []
    # Le membre qui a bouge est NOMME : une phrase seule obligerait a deviner.
    assert [m["raw_import_id"] for m in exc.value.members] == ["inbraw_2"]


def test_a_partly_refused_scope_says_partial_and_not_landed(monkeypatch):
    """Trois mots, pas deux. Rendre << landed >> cacherait les membres refuses ;
    rendre << rejected >> cacherait ceux qui ont publie.
    """
    from core.inbound_reprocess import execute_reprocess_scope

    ids = ["inbraw_1", "inbraw_2"]
    conn, captured = _wire_execution(
        monkeypatch,
        rows={"config": {"source_owner": {}}},
        raws={i: _raw(i, uri=f"memory://{i}") for i in ids},
        ingest_results={
            "inbraw_2": {"blocked": True, "reason": "empty_import_blocked"}
        },
    )
    out = execute_reprocess_scope(
        conn,
        datastream_id="ds_1",
        raw_import_ids=ids,
        actor="operator",
        idempotency_key="ik-partial",
        reason="mapping repaired",
        store=_MemoryStore({f"memory://{i}": _CSV for i in ids}),
    )
    assert out["status"] == "partial"
    assert out["scope"] == {
        "state": "counted",
        "confirmed": 2,
        "landed": 1,
        "refused": 1,
    }
    refused = [m for m in out["members"] if m["status"] == "rejected"]
    assert refused[0]["error_code"] == "empty_import_blocked"
    # La raison survit sur CHAQUE membre, y compris celui qui a refuse.
    assert all(m["reason"] == "mapping repaired" for m in out["members"])


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"actor": ""}, "actor"),
        ({"idempotency_key": ""}, "idempotency_key"),
        ({"reason": ""}, "reason"),
        ({"reason": "   "}, "reason"),
        ({"raw_import_ids": []}, "non-empty"),
        ({"raw_import_ids": ["a", "a"]}, "duplicates"),
    ],
)
def test_a_malformed_scope_execution_raises_before_any_work(kwargs, fragment):
    """Une portee sans raison est un changement inexplique de donnees publiees,
    N fois.
    """
    from core.inbound_reprocess import ReprocessValidationError, execute_reprocess_scope

    base = dict(
        datastream_id="ds_1",
        raw_import_ids=["inbraw_1"],
        actor="operator",
        idempotency_key="ik-1",
        reason="mapping repaired",
    )
    base.update(kwargs)
    rows: dict = {}
    conn = _Conn(rows)

    with pytest.raises(ReprocessValidationError, match=fragment):
        execute_reprocess_scope(conn, **base)
    assert "_sql" not in rows


# ===========================================================================
# Montage. Une commande que rien ne route n'est pas une commande.
# ===========================================================================


def test_the_scope_routes_are_mounted_with_both_verbs():
    from core.admin_api import router

    path = (
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/reprocess-scope"
    )
    verbs = {
        m
        for r in router.routes
        if getattr(r, "path", None) == path
        for m in (r.methods or set())
        if m in ("GET", "POST")
    }
    assert verbs == {"GET", "POST"}, verbs


def test_the_scope_execution_never_writes_to_the_retained_evidence():
    """<< original bytes and prior executions remain immutable >> vaut aussi pour
    la portee : elle multiplie les lectures, jamais les ecritures.
    """
    import inspect

    from core import inbound_reprocess

    source = inspect.getsource(inbound_reprocess)
    for writer in (".put(", ".delete(", "UPDATE app.inbound_raw_imports"):
        assert writer not in source, writer
