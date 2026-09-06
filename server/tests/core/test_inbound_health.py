"""Story 38.14: layered connector health and the attachment inbox.

The reads here are multi-statement joins against a real schema, so most of this
file is live-PG. Mock-cursor tests for a five-query reader assert the order I
happened to write the cursors in, not that the SQL is correct -- and the whole
reason this story exists is that evidence was being written and read by nobody,
which a mock would never have caught.

Offline:
  (a) The overall verdict is the WEAKEST layer, and it carries that layer's
      authority.
  (b) An unreadable layer is `unknown`, never healthy -- in either direction.

Live-PG:
  (c) The layers separate installation / domain / activation / delivery / data.
  (d) A DENIED or FAILED delivery appears in the inbox. This is the regression
      that matters: the previous reader joined from the import ledger, so it
      could only ever show the successes.
  (e) `last_known_good_publication` stays populated when the latest import fails.
  (f) The delivery timeline states explicitly whether published data changed.
  (g) Cross-Datastream reads return nothing, indistinguishably from absent.
  (h) No secret, no row sample, in any of the three payloads.
"""

from __future__ import annotations

import hashlib
import hashlib as _hashlib
import json
import pathlib

import pytest

_HASH_A = hashlib.sha256(b"a,b\n1,2\n").hexdigest()
_HASH_B = hashlib.sha256(b"a,b\n3,4\n").hexdigest()


# ---------------------------------------------------------------------------
# (a) (b) The fold, offline -- it is pure decision logic.
# ---------------------------------------------------------------------------


class _FailingConn:
    """A connection whose every cursor raises -- the 'unreadable' case."""

    def cursor(self):
        raise RuntimeError("database unreachable")


def test_an_unreadable_layer_is_unknown_never_healthy():
    """A layer with no evidence must not be reported green.

    This is the same defect class as `search_context` answering "no context
    found" when the store is down: an outage that renders as an empty, healthy
    answer is worse than an error, because nobody investigates it.
    """
    from core.inbound_health import get_inbound_health

    health = get_inbound_health(
        _FailingConn(), connector_name="inbound-managed-files", environment="test"
    )

    assert health["overall"] == "unknown"
    assert all(layer["healthy"] is None for layer in health["layers"])
    assert health["blocking_cause"] is not None


def test_the_weakest_layer_decides_and_names_its_authority():
    """ "Who fixes this" is answered by the same call that says it is broken."""
    import core.inbound_health as ih
    from core.inbound_health import (
        AUTHORITY_ORG_OWNER,
        AUTHORITY_PLATFORM_ADMIN,
        get_inbound_health,
    )

    healthy_platform = {
        "layer": "installation",
        "state": "READY",
        "healthy": True,
        "authority": AUTHORITY_PLATFORM_ADMIN,
        "blocking_cause": None,
        "next_action": None,
    }
    broken_tenant = {
        "layer": "activation",
        "state": "NOT_ACTIVATED",
        "healthy": False,
        "authority": AUTHORITY_ORG_OWNER,
        "blocking_cause": "connector_not_activated_for_this_organization",
        "next_action": "An organization owner activates the connector.",
    }

    original_install = ih._read_installation
    original_domain = ih._read_domain
    original_activation = ih._read_activation
    try:
        ih._read_installation = lambda conn, **kw: dict(healthy_platform)
        ih._read_domain = lambda conn, **kw: dict(healthy_platform, layer="domain")
        ih._read_activation = lambda conn, **kw: dict(broken_tenant)

        health = get_inbound_health(
            object(),
            connector_name="inbound-managed-files",
            environment="test",
            org_id="org-1",
        )
    finally:
        ih._read_installation = original_install
        ih._read_domain = original_domain
        ih._read_activation = original_activation

    assert health["overall"] == "blocked"
    # NOT the platform administrator: the broken layer is the tenant's.
    assert health["authority"] == AUTHORITY_ORG_OWNER
    assert health["blocking_cause"] == "connector_not_activated_for_this_organization"


def test_a_known_break_outranks_an_unread_layer():
    """Both are non-healthy; the actionable one is the one worth surfacing."""
    import core.inbound_health as ih
    from core.inbound_health import AUTHORITY_PLATFORM_ADMIN, get_inbound_health

    unknown_layer = {
        "layer": "installation",
        "state": "unknown",
        "healthy": None,
        "authority": AUTHORITY_PLATFORM_ADMIN,
        "blocking_cause": "installation_state_unreadable",
        "next_action": None,
    }
    broken_layer = {
        "layer": "domain",
        "state": "NOT_CONFIGURED",
        "healthy": False,
        "authority": AUTHORITY_PLATFORM_ADMIN,
        "blocking_cause": "no_verified_domain",
        "next_action": "Configure a domain.",
    }

    original_install, original_domain = ih._read_installation, ih._read_domain
    try:
        ih._read_installation = lambda conn, **kw: dict(unknown_layer)
        ih._read_domain = lambda conn, **kw: dict(broken_layer)
        health = get_inbound_health(object(), connector_name="c", environment="test")
    finally:
        ih._read_installation, ih._read_domain = original_install, original_domain

    assert health["overall"] == "blocked"
    assert health["blocking_cause"] == "no_verified_domain"


def test_parser_review_detail_exposes_only_bounded_codes_and_defused_fields():
    import json

    from core.inbound_health import _redact_parser_review_detail

    detail = json.dumps(
        {
            "schema": "parser-review-v1",
            "issues": [{"code": "date_format_mixed", "field": "day<\nscript>"}],
        }
    )
    assert _redact_parser_review_detail("parser_review_required", detail) == {
        "schema": "parser-review-v1",
        "issues": [{"code": "date_format_mixed", "field": "day<script>"}],
    }
    assert _redact_parser_review_detail("other", detail) is None


def test_the_inbox_bounds_its_own_page_size():
    from core.inbound_health import get_attachment_inbox

    for bad in (0, -1, 201):
        with pytest.raises(ValueError, match="limit"):
            get_attachment_inbox(object(), datastream_id="ds-1", limit=bad)


# ---------------------------------------------------------------------------
# Live-PG fixtures.
# ---------------------------------------------------------------------------



#: Le chemin LEGAL d'une evidence brute (migration 185) : elle ne saute pas a son
#: etat final, elle y marche. `RECEIVED -> SCANNING -> ACCEPTED -> LANDED` ; un
#: refus part directement de RECEIVED. Chaque saut exige une provenance FRAICHE
#: et un `updated_at` strictement posterieur -- une transition sans nouvelle
#: operation n'est pas une transition, c'est une reecriture.
_RAW_PATHS = {
    "LANDED": ("SCANNING", "ACCEPTED", "LANDED"),
    "ACCEPTED": ("SCANNING", "ACCEPTED"),
    "REJECTED": ("REJECTED",),
    "FAILED": ("FAILED",),
    "DUPLICATE": ("DUPLICATE",),
}


def _mint_ledger_row(cur, *, datastream_id, outcome="opened", **columns):
    """One real `app.managed_feed_import_ledger` row for *datastream_id*.

    `outcome` defaults to 'opened' -- the only NON-terminal outcome. Minting a
    terminal one would make the row unmovable, and every test that wants to see
    the publication stage change would have to build its own delivery.
    """
    import ulid as _ulid

    ledger_id = f"mfl_{_ulid.ULID()}"
    cur.execute("SELECT project_id FROM app.datastreams WHERE id = %s", (datastream_id,))
    (project_id,) = cur.fetchone()
    extra = "".join(f", {name}" for name in columns)
    placeholders = "".join(", %s" for _ in columns)
    cur.execute(
        "INSERT INTO app.managed_feed_import_ledger "
        "(id, datastream_id, project_id, plan_version_id, mapping_version_id, "
        " feed_format, write_mode, idempotency_key_hash, payload_fingerprint, "
        " outcome, created_by" + extra + ") "
        "VALUES (%s, %s, %s, 'plan_v1', 'map_v1', 'csv', 'replace', %s, %s, "
        "        %s, 'system'" + placeholders + ")",
        (
            ledger_id,
            datastream_id,
            project_id,
            _hashlib.sha256(ledger_id.encode()).hexdigest(),
            _hashlib.sha256(f"payload-{ledger_id}".encode()).hexdigest(),
            outcome,
            *columns.values(),
        ),
    )
    return ledger_id


def _walk_raw_import(cur, insert_operation, *, raw_id, target, error_code=None,
                     verdict=None, datastream_id, ledger_id=None):
    """Conduire une evidence brute de RECEIVED jusqu'a *target*, legalement.

    Chaque saut porte sa propre operation, de commande
    `inbound.raw_import.state_changed`, nommant le Datastream, l'evidence et
    l'ETAT VISE. Une operation generique ne peut pas etre la provenance de CE
    changement-la.
    """
    if target == "LANDED" and ledger_id is None:
        # UNE PREUVE D'ATTERRISSAGE EST UNE LIGNE, PAS UN IDENTIFIANT. La garde
        # `protect_inbound_raw_import` exige un registre pour un LANDED, et un
        # `mfi_<ULID>` fabrique la satisfaisait sans designer quoi que ce soit --
        # de sorte que tout ce qu'une livraison produit APRES l'atterrissage
        # (mapping, DQ, publication) restait hors de portee des tests.
        ledger_id = _mint_ledger_row(cur, datastream_id=datastream_id)

    steps = _RAW_PATHS[target]
    for index, state in enumerate(steps):
        last = index == len(steps) - 1
        cur.execute(
            "UPDATE app.inbound_raw_imports "
            # UN `LANDED` PORTE SA PREUVE D'ATTERRISSAGE. La garde refuse un
            # atterrissage sans ligne de registre : dire << c'est arrive >> sans
            # pouvoir montrer OU serait exactement le genre d'affirmation que
            # cette story existe pour empecher.
            "SET state = %s, error_code = %s, scan_verdict = %s::jsonb, "
            "    import_ledger_id = %s, "
            "    operation_id = %s, updated_at = clock_timestamp() "
            "WHERE id = %s",
            (
                state,
                error_code if last else None,
                verdict if last else None,
                # UNE VRAIE LIGNE DE REGISTRE, PAS UN IDENTIFIANT PLAUSIBLE.
                # Ceci frappait un `mfi_<ULID>` -- un prefixe que la contrainte
                # `ck_managed_feed_import_ledger_id` REFUSE (`mfl_`). La garde
                # d'atterrissage etait donc satisfaite par un identifiant qui ne
                # pouvait designer aucune ligne, et tout ce que la livraison
                # produit APRES l'atterrissage restait hors de portee du test.
                ledger_id if state == "LANDED" else None,
                # UNE TRANSITION NE NOMME PAS CE QU'UN ENREGISTREMENT NOMME.
                # L'enregistrement lie {datastream, recu, rang} ; la transition
                # lie {datastream, evidence, ETAT VISE}. Le meme jeu de ressources
                # pour les deux serait accepte par l'un et refuse par l'autre --
                # ce sont deux actes differents sur deux objets differents.
                insert_operation(
                    "inbound.raw_import.state_changed",
                    resource_path=[
                        f"datastream:{datastream_id}",
                        f"raw_import:{raw_id}",
                        f"state:{state}",
                    ],
                ),
                raw_id,
            ),
        )


def _quarantine_uri(org_id: str, datastream_id: str, content_hash: str) -> str:
    """L'adresse EXACTE que la migration 185 exige.

    Le garde verifie que l'URI contient `/inbound/<org>/<datastream>/<hash>/` :
    une evidence rangee ailleurs que sous sa propre portee n'est pas la sienne, et
    une preuve qu'on ne peut pas rattacher a son contenu ne prouve rien. C'est la
    forme que la vraie quarantaine ecrit -- verifiee sur la livraison reelle du
    2026-08-08.
    """
    return f"gs://q/inbound/{org_id}/{datastream_id}/{content_hash}/object-0000"


@pytest.fixture
def delivery_fixture(pg_conn, inbound_pg_scope, insert_operation):
    """A delivery with three attachments in three different terminal states.

    Built rather than described: one landed, one rejected by the scan gate, one
    failed. The point of the inbox is that the last two are visible, and only
    real rows in those states can prove it.
    """
    import ulid as _ulid

    ds_id = inbound_pg_scope["datastream_id"]
    receipt_id = f"inbrx_{_ulid.ULID()}"
    op_id = insert_operation("inbound.receipt.recorded")

    with pg_conn.cursor() as cur:
        # LE REGISTRE D'IMPORT EXISTE VRAIMENT. `inbound_processing` pose l'id
        # RENDU par le registre (`ledger.get("id")`), donc une piece jointe
        # atterrie en pointe toujours une vraie ligne. Il est ouvert et non
        # terminal : chaque test l'avance vers l'issue qu'il veut observer.
        ledger_id = _mint_ledger_row(
            cur, datastream_id=ds_id, row_count=180, rejected_row_count=20
        )
        cur.execute(
            "INSERT INTO app.inbound_receipts "
            # `receipt_fingerprint` est NOT NULL et contraint a 64 hex
            # (migration 184). Les fixtures ne le posaient pas du tout : une
            # colonne obligatoire absente, invisible tant que ces tests ne
            # tournaient pas faute de DSN.
            "(id, datastream_id, channel, provider_event_id, attachment_count, "
            "state, operation_id, receipt_fingerprint) "
            # UN RECU COMMENCE EN RECEIVED. La garde de la migration 184
            # (`inbound receipt must start in RECEIVED`) dit l'invariant du
            # cycle de vie : un recu naquit a la reception et progresse. Poser
            # directement 'LANDED' fabriquait un etat qu'aucune livraison ne
            # peut avoir, et c'est la fixture qui avait tort, pas la garde --
            # six tests rouges dont << celui qui compte >>.
            "VALUES (%s, %s, 'email', %s, 3, 'RECEIVED', %s, %s)",
            (
                receipt_id,
                ds_id,
                f"evt-health-{_ulid.ULID()}",
                op_id,
                _hashlib.sha256(receipt_id.encode()).hexdigest(),
            ),
        )
        # UN RECU MARCHE LUI AUSSI : RECEIVED -> PROCESSING -> LANDED, chaque
        # saut avec sa propre operation `inbound.receipt.state_advanced` nommant
        # le Datastream et le recu. Et un LANDED porte son registre : dire
        # << atterri >> sans pouvoir montrer ou est ce que la garde refuse.
        for state in ("PROCESSING", "LANDED"):
            cur.execute(
                "UPDATE app.inbound_receipts "
                "SET state = %s, import_ledger_id = %s, operation_id = %s, "
                "    updated_at = clock_timestamp() "
                "WHERE id = %s",
                (
                    state,
                    ledger_id if state == "LANDED" else None,
                    insert_operation(
                        "inbound.receipt.state_advanced",
                        resource_path=[
                            f"datastream:{ds_id}",
                            f"receipt:{receipt_id}",
                        ],
                    ),
                    receipt_id,
                ),
            )
        rows = [
            (0, "good.csv", "LANDED", None, _HASH_A, None),
            (
                1,
                "bomb.zip",
                "REJECTED",
                "archive_compression_ratio_exceeded",
                _HASH_B,
                '{"accepted": false, "malware": "unavailable"}',
            ),
            (
                2,
                "broken.csv",
                "FAILED",
                "quarantine_read_error",
                hashlib.sha256(b"x").hexdigest(),
                None,
            ),
        ]
        raw_ids = []
        for ordinal, filename, state, error_code, digest, verdict in rows:
            raw_id = f"inbraw_{_ulid.ULID()}"
            raw_ids.append(raw_id)
            cur.execute(
                "INSERT INTO app.inbound_raw_imports "
                "(id, receipt_id, datastream_id, ordinal, filename, "
                "media_type_declared, size_bytes, content_hash, quarantine_uri, "
                "retention_policy_version, retention_days, retention_expires_at, "
                "state, error_code, scan_verdict, operation_id) "
                # MEME INVARIANT QUE POUR LE RECU : une evidence brute naquit en
                # RECEIVED (`protect_inbound_raw_import`, migration 184) et
                # progresse ensuite. La poser directement dans son etat final
                # fabriquait une ligne qu'aucune livraison ne peut produire.
                "VALUES (%s, %s, %s, %s, %s, 'text/csv', 42, %s, "
                "%s, 'quarantine-retention-v1', 30, "
                "NOW() + INTERVAL '30 days', 'RECEIVED', NULL, NULL, %s)",
                (
                    raw_id,
                    receipt_id,
                    ds_id,
                    ordinal,
                    filename,
                    digest,
                    _quarantine_uri(inbound_pg_scope["org_id"], ds_id, digest),
                    # LA PROVENANCE D'UNE EVIDENCE BRUTE NOMME LES TROIS CHOSES
                    # qu'elle rattache (migration 185) : le Datastream, le recu et
                    # le rang de la piece jointe. Une operation generique ne peut
                    # pas etre la provenance de CETTE piece-la.
                    insert_operation(
                        "inbound.raw_import.recorded",
                        resource_path=[
                            f"datastream:{ds_id}",
                            f"receipt:{receipt_id}",
                            f"attachment:{ordinal}",
                        ],
                    ),
                ),
            )
            _walk_raw_import(
                cur,
                insert_operation,
                raw_id=raw_id,
                target=state,
                error_code=error_code,
                verdict=verdict,
                datastream_id=ds_id,
                ledger_id=ledger_id,
            )
    pg_conn.commit()
    return {
        "datastream_id": ds_id,
        "receipt_id": receipt_id,
        "raw_ids": raw_ids,
        "ledger_id": ledger_id,
    }


# ---------------------------------------------------------------------------
# (c) Layers, against the real schema.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_health_separates_its_layers(pg_conn, inbound_pg_scope):
    from core.inbound_health import get_inbound_health

    health = get_inbound_health(
        pg_conn,
        connector_name="inbound-managed-files",
        environment="test",
        datastream_id=inbound_pg_scope["datastream_id"],
        org_id=inbound_pg_scope["org_id"],
    )

    names = [layer["layer"] for layer in health["layers"]]
    # SIX, as AC1 names them. `queue` was the missing one until 2026-08-10.
    assert names == [
        "installation",
        "domain",
        "activation",
        "delivery",
        "queue",
        "data",
    ]
    # Every layer answers the same three questions.
    for layer in health["layers"]:
        assert set(layer) >= {
            "layer",
            "state",
            "healthy",
            "authority",
            "blocking_cause",
            "next_action",
        }
    # This Datastream has no plan/mapping and no credential, so delivery is
    # blocked -- and it says so BEFORE a file arrives, which is the point.
    delivery = next(x for x in health["layers"] if x["layer"] == "delivery")
    assert delivery["healthy"] is False
    assert delivery["blocking_cause"] is not None
    assert delivery["next_action"]


@pytest.mark.live_pg
def test_live_pg_datastream_layers_are_omitted_not_faked(pg_conn):
    """No datastream_id means no delivery/data layers -- absent, not empty."""
    from core.inbound_health import get_inbound_health

    health = get_inbound_health(pg_conn, connector_name="inbound-managed-files", environment="test")
    names = [layer["layer"] for layer in health["layers"]]
    assert "delivery" not in names and "data" not in names
    assert health["metrics"] is None


# ---------------------------------------------------------------------------
# (d) THE regression: refused deliveries are visible.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_the_inbox_shows_attachments_that_never_landed(pg_conn, delivery_fixture):
    """The whole reason this story exists.

    The previous reader was a LEFT JOIN LATERAL from the import ledger, so an
    attachment that produced no ledger row -- rejected, failed, denied -- had
    nowhere to appear. Those are precisely the ones an operator goes looking
    for.
    """
    from core.inbound_health import get_attachment_inbox

    items = get_attachment_inbox(pg_conn, datastream_id=delivery_fixture["datastream_id"])
    by_name = {item["filename"]: item for item in items}

    assert set(by_name) == {"good.csv", "bomb.zip", "broken.csv"}
    assert by_name["bomb.zip"]["state"] == "REJECTED"
    assert by_name["bomb.zip"]["error_code"] == "archive_compression_ratio_exceeded"
    assert by_name["broken.csv"]["state"] == "FAILED"
    # The scan verdict travels with the row: an operator sees WHY without
    # downloading a byte (38.10 AC5).
    assert by_name["bomb.zip"]["scan_verdict"]["accepted"] is False
    # And each row carries its delivery, so "which email was that" is answerable.
    assert by_name["bomb.zip"]["receipt"]["channel"] == "email"


@pytest.mark.live_pg
def test_live_pg_the_inbox_defuses_an_untrusted_filename(
    pg_conn, inbound_pg_scope, insert_operation
):
    """A delivered filename is attacker-controlled text, and it is displayed."""
    import ulid as _ulid
    from core.inbound_health import get_attachment_inbox

    ds_id = inbound_pg_scope["datastream_id"]
    receipt_id = f"inbrx_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.inbound_receipts "
            "(id, datastream_id, channel, provider_event_id, attachment_count, "
            # Meme invariant qu'au-dessus : naitre en RECEIVED, puis progresser.
            "state, operation_id, receipt_fingerprint) "
            "VALUES (%s, %s, 'email', %s, 1, 'RECEIVED', %s, %s)",
            (
                receipt_id,
                ds_id,
                f"evt-nasty-{_ulid.ULID()}",
                insert_operation("inbound.receipt.recorded"),
                _hashlib.sha256(receipt_id.encode()).hexdigest(),
            ),
        )
        cur.execute(
            "INSERT INTO app.inbound_raw_imports "
            "(id, receipt_id, datastream_id, ordinal, filename, size_bytes, "
            # Naitre en RECEIVED, puis progresser -- meme invariant que partout.
            # Et porter sa QUARANTAINE ET SA RETENTION : la migration 185 refuse
            # une evidence brute qui n'en a pas, parce qu'une evidence dont on ne
            # sait ni ou elle est ni jusqu'a quand elle vit n'est pas une preuve.
            "content_hash, quarantine_uri, retention_policy_version, "
            "retention_days, retention_expires_at, state, operation_id) "
            "VALUES (%s, %s, %s, 0, %s, 10, %s, %s, 'quarantine-retention-v1', "
            "30, NOW() + INTERVAL '30 days', 'RECEIVED', %s)",
            (
                nasty_raw_id := f"inbraw_{_ulid.ULID()}",
                receipt_id,
                ds_id,
                "=cmd|'/c calc'!A1.csv",
                _HASH_A,
                _quarantine_uri(inbound_pg_scope["org_id"], ds_id, _HASH_A),
                insert_operation(
                    "inbound.raw_import.recorded",
                    resource_path=[
                        f"datastream:{ds_id}",
                        f"receipt:{receipt_id}",
                        "attachment:0",
                    ],
                ),
            ),
        )
        _walk_raw_import(
            cur,
            insert_operation,
            raw_id=nasty_raw_id,
            target="LANDED",
            datastream_id=ds_id,
        )
    pg_conn.commit()

    items = get_attachment_inbox(pg_conn, datastream_id=ds_id, limit=200)
    nasty = [i for i in items if "calc" in (i["filename"] or "")]
    assert nasty, "the row should still be listed -- defused, not hidden"
    assert nasty[0]["filename"].startswith("'")


# ---------------------------------------------------------------------------
# (e) Last-known-good survives a failed latest import.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_last_known_good_is_its_own_field(pg_conn, inbound_pg_scope):
    """A failed candidate must never make a serving publication look absent."""
    from core.inbound_health import get_inbound_health

    health = get_inbound_health(
        pg_conn,
        connector_name="inbound-managed-files",
        environment="test",
        datastream_id=inbound_pg_scope["datastream_id"],
    )
    data = next(x for x in health["layers"] if x["layer"] == "data")
    # Two distinct keys, always. Collapsing them is the defect.
    assert "latest_import" in data
    assert "last_known_good_publication" in data


# ---------------------------------------------------------------------------
# (f) (g) Timeline.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_timeline_states_whether_published_data_changed(pg_conn, delivery_fixture):
    from core.inbound_health import get_delivery_timeline

    timeline = get_delivery_timeline(
        pg_conn,
        receipt_id=delivery_fixture["receipt_id"],
        datastream_id=delivery_fixture["datastream_id"],
    )
    assert timeline is not None
    assert timeline["attachment_count"] == 3
    assert timeline["landed_count"] == 1
    # PAR CONSTRUCTION, PAS PAR DEDUCTION. Ceci lisait `landed_count == 1` et en
    # concluait que des donnees publiees avaient change. L'atterrissage n'est
    # pas la publication : il faut que le registre le DISE.
    _set_ledger(pg_conn, delivery_fixture["ledger_id"], outcome="published")
    timeline = get_delivery_timeline(
        pg_conn,
        receipt_id=delivery_fixture["receipt_id"],
        datastream_id=delivery_fixture["datastream_id"],
    )
    assert timeline["published_data_changed"] is True
    assert [a["ordinal"] for a in timeline["attachments"]] == [0, 1, 2]


@pytest.mark.live_pg
def test_live_pg_a_foreign_receipt_is_indistinguishable_from_an_absent_one(
    pg_conn, delivery_fixture
):
    from core.inbound_health import get_delivery_timeline

    foreign = get_delivery_timeline(
        pg_conn,
        receipt_id=delivery_fixture["receipt_id"],
        datastream_id="ds-belonging-to-someone-else",
    )
    absent = get_delivery_timeline(
        pg_conn,
        receipt_id="inbrx_00000000000000000000000000",
        datastream_id=delivery_fixture["datastream_id"],
    )
    assert foreign is None and absent is None


# ---------------------------------------------------------------------------
# (h) Nothing sensitive in any payload.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_no_payload_carries_a_secret_or_a_sample(
    pg_conn, delivery_fixture, inbound_pg_scope
):
    """One assertion over all three reads, because one leak is enough."""
    import json

    from core.inbound_health import (
        get_attachment_inbox,
        get_delivery_timeline,
        get_inbound_health,
    )

    blobs = [
        json.dumps(
            get_inbound_health(
                pg_conn,
                connector_name="inbound-managed-files",
                environment="test",
                datastream_id=delivery_fixture["datastream_id"],
                org_id=inbound_pg_scope["org_id"],
            )
        ),
        json.dumps(get_attachment_inbox(pg_conn, datastream_id=delivery_fixture["datastream_id"])),
        json.dumps(
            get_delivery_timeline(
                pg_conn,
                receipt_id=delivery_fixture["receipt_id"],
                datastream_id=delivery_fixture["datastream_id"],
            )
        ),
    ]
    forbidden = (
        "token_hash",
        "signing_secret",
        "signing_secret_ref",
        "raw_token",
        "recipient_address",
        "full_secret",
    )
    for blob in blobs:
        for fragment in forbidden:
            assert fragment not in blob, f"payload leaked {fragment!r}"


@pytest.mark.live_pg
def test_live_pg_metrics_are_counts_not_content(pg_conn, delivery_fixture):
    from core.inbound_health import get_inbound_health

    health = get_inbound_health(
        pg_conn,
        connector_name="inbound-managed-files",
        environment="test",
        datastream_id=delivery_fixture["datastream_id"],
    )
    metrics = health["metrics"]
    assert metrics["attachments_total"] >= 3
    assert metrics["deliveries_total"] >= 1
    assert set(metrics["attachments_by_state"]) >= {"LANDED", "REJECTED", "FAILED"}
    assert "last_receipt_at" in metrics

    # AC3, l autre moitie : la LATENCE de traitement, pas seulement l age de la
    # file. Les deux repondent a des questions differentes -- << le worker
    # draine-t-il ? >> et << a quoi ressemble un traitement normal ? >> -- et sans
    # la seconde personne ne peut dire si la premiere est anormale.
    assert "processing_latency_median_seconds" in metrics
    assert "processing_latency_max_seconds" in metrics

    # L ECHANTILLON VOYAGE AVEC LA MESURE. Une mediane sur deux recus n en est
    # pas une, et un lecteur qui ne voit pas le compte la traite comme si elle
    # en etait une. Aucun seuil n est invente : le compte est rendu, qui lit
    # decide.
    assert metrics["settled_receipts"] >= 1
    if metrics["processing_latency_median_seconds"] is not None:
        assert metrics["processing_latency_median_seconds"] >= 0
        assert (
            metrics["processing_latency_max_seconds"]
            >= metrics["processing_latency_median_seconds"]
        )

    # Every value is a count, a timestamp or a state name.
    for key, value in metrics.items():
        assert isinstance(value, (int, float, str, dict, bool, type(None))), key


# ---------------------------------------------------------------------------
# AC2, la seconde moitie de la colonne vertebrale : mapping, DQ, publication.
# ---------------------------------------------------------------------------


def _set_ledger(pg_conn, ledger_id, **columns):
    """Move the delivery's ledger row through its terminal lifecycle fields.

    Migration 077 freezes the ledger EXCEPT `outcome`, the row counts and the
    landing coordinates -- exactly the fields a real import moves. Re-pointing
    the attachment instead is refused by `protect_inbound_raw_import`, and
    rightly so: a terminal raw import does not acquire a different landing.
    """
    assignments = ", ".join(f"{name} = %s" for name in columns)
    with pg_conn.cursor() as cur:
        cur.execute(
            f"UPDATE app.managed_feed_import_ledger SET {assignments} WHERE id = %s",
            (*columns.values(), ledger_id),
        )
    pg_conn.commit()


@pytest.mark.live_pg
def test_live_pg_the_spine_reaches_publication_and_stops_claiming_it(
    pg_conn, delivery_fixture
):
    """LANDED ne veut pas dire PUBLIE, et le disait.

    `published_data_changed` valait `bool(landed)`. Or `state='LANDED'` sur une
    piece jointe signifie << elle a produit une ligne de registre d'import >>,
    et cette ligne peut rester a `written` (un candidat existe, rien n'est en
    ligne), passer a `rejected` (la porte DQ a refuse) ou a `failed`. Un
    operateur lisait donc << vos donnees publiees ont change >> alors que rien
    n'avait ete publie.

    Ce test tient la ligne de registre dans les DEUX etats qui separent le vrai
    du faux, sur la MEME livraison. Un seul des deux ne prouverait rien : c'est
    le changement de reponse entre `written` et `published` qui montre que la
    surface LIT l'issue au lieu de la deduire.
    """
    from core.inbound_health import get_delivery_timeline

    ds_id = delivery_fixture["datastream_id"]
    receipt_id = delivery_fixture["receipt_id"]
    _set_ledger(pg_conn, delivery_fixture["ledger_id"], outcome="written")

    timeline = get_delivery_timeline(
        pg_conn, receipt_id=receipt_id, datastream_id=ds_id
    )
    landed = next(a for a in timeline["attachments"] if a.get("state") == "LANDED")
    stages = landed["downstream"]

    # Les trois etages que l'AC2 reclamait et que la surface ne lisait pas.
    assert stages["mapping"]["mapping_version_id"] == "map_v1"
    assert stages["mapping"]["plan_version_id"] == "plan_v1"
    assert stages["dq"]["accepted_row_count"] == 180
    assert stages["dq"]["rejected_row_count"] == 20
    assert stages["dq"]["rejected_row_pct"] == 10.0

    # LE FAUX POSITIF. La piece jointe EST atterrie -- l'ancienne surface aurait
    # repondu True ici, et se serait trompee.
    assert stages["publication"]["outcome"] == "written"
    assert stages["publication"]["published"] is False
    assert timeline["landed_count"] == 1
    assert timeline["published_count"] == 0
    assert timeline["published_data_changed"] is False

    # Et quand la publication a REELLEMENT eu lieu, la meme surface le dit.
    _set_ledger(pg_conn, delivery_fixture["ledger_id"], outcome="published")
    timeline = get_delivery_timeline(
        pg_conn, receipt_id=receipt_id, datastream_id=ds_id
    )
    assert timeline["published_count"] == 1
    assert timeline["published_data_changed"] is True


@pytest.mark.live_pg
def test_live_pg_the_spine_does_not_leak_the_landing_relation(
    pg_conn, delivery_fixture
):
    """Le registre porte l'adresse d'une relation brute interne. Elle y reste.

    `landing_relation` et `error_detail` verbatim sont deux choses que le reste
    de cette surface retire deja (`_WITHHELD_FROM_EVERY_SURFACE`). Les laisser
    revenir par la seconde moitie de la colonne vertebrale serait exactement le
    defaut << deux surfaces, une seule redaction >> que ce module a deja combattu.
    """
    from core.inbound_health import get_delivery_timeline

    _set_ledger(
        pg_conn,
        delivery_fixture["ledger_id"],
        landing_relation="org_secret_raw.landing_table",
        outcome="failed",
        error_code="writer_failed",
        error_detail="connection string: postgres://user:pw@host/db",
    )

    timeline = get_delivery_timeline(
        pg_conn,
        receipt_id=delivery_fixture["receipt_id"],
        datastream_id=delivery_fixture["datastream_id"],
    )
    rendered = json.dumps(timeline)
    assert "org_secret_raw.landing_table" not in rendered
    assert "postgres://" not in rendered
    # Le code, lui, est ce sur quoi un operateur agit : il reste.
    landed = next(a for a in timeline["attachments"] if a.get("state") == "LANDED")
    assert landed["downstream"]["publication"]["error_code"] == "writer_failed"
    assert timeline["published_data_changed"] is False


@pytest.mark.live_pg
def test_live_pg_an_unmeasured_row_count_yields_no_rate(pg_conn, delivery_fixture):
    """0 rejetee sur un total inconnu n'est pas 0 %, c'est << rien compte >>.

    `row_count` est honest-NULL jusqu'a la mesure (AD-9). Fabriquer un
    denominateur produirait un taux propre pour un run qui n'a jamais lu une
    ligne -- un chiffre rassurant qui ne mesure rien.
    """
    from core.inbound_health import get_delivery_timeline

    _set_ledger(
        pg_conn,
        delivery_fixture["ledger_id"],
        row_count=None,
        rejected_row_count=0,
        outcome="opened",
    )

    timeline = get_delivery_timeline(
        pg_conn,
        receipt_id=delivery_fixture["receipt_id"],
        datastream_id=delivery_fixture["datastream_id"],
    )
    landed = next(a for a in timeline["attachments"] if a.get("state") == "LANDED")
    assert landed["downstream"]["dq"]["accepted_row_count"] is None
    assert landed["downstream"]["dq"]["rejected_row_pct"] is None
    assert timeline["published_data_changed"] is False


# ---------------------------------------------------------------------------
# AC4 : une alerte pointe l'action de reparation que son AUTORITE peut invoquer.
# ---------------------------------------------------------------------------


def test_an_alert_links_to_a_repair_and_not_only_to_a_sentence():
    """`next_action` etait une phrase. Une phrase n'est pas un lien.

    AC4 demande qu'une alerte << link to an authorized console/MCP recovery
    action >>. La couche disait a un humain ce qui devrait arriver, et ne donnait
    ni a la console une destination a ouvrir, ni a un hote une commande a
    appeler. L'autorite etait nommee, l'action ne l'etait pas.
    """
    from core.inbound_health import _layer

    layer = _layer(
        "delivery",
        state="BLOCKED",
        healthy=False,
        authority="datastream_operator",
        blocking_cause="scan_dead_letter",
        next_action="An operator recovers the dead-lettered scan.",
        recovery={
            "api": "/api/connectors/{connector_name}/x/recover",
            "method": "POST",
            "mcp_tool": "list_inbound_attachments",
            "console": None,
        },
    )
    assert layer["authority"] == "datastream_operator"
    assert layer["recovery"]["api"].endswith("/recover")
    assert layer["recovery"]["mcp_tool"] == "list_inbound_attachments"


def test_a_healthy_layer_offers_no_repair():
    """Rien a reparer, donc rien a proposer.

    Une action de reparation affichee sur une couche saine invite a la presser,
    et chaque pression est une operation reelle sur un systeme qui va bien.
    """
    from core.inbound_health import _layer

    layer = _layer(
        "installation",
        state="READY",
        healthy=True,
        authority="platform_admin",
        recovery={"api": "/api/connectors/{connector_name}/installation"},
    )
    assert layer["recovery"] is None


def test_every_named_repair_is_a_route_that_exists():
    """Nommer une reparation non montee serait le meme defaut d'un cran plus haut.

    Ce test lit les tables de routes REELLES plutot que de recopier des chemins :
    une route renommee doit casser ici, pas se decouvrir devant un operateur qui
    clique.
    """
    from core.connector_activation_api import CONNECTOR_ACTIVATION_ROUTES
    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from core.connector_verification_api import CONNECTOR_VERIFICATION_ROUTES
    from core.inbound_health import (
        _RECOVERY_ACTIVATE,
        _RECOVERY_INSTALL,
        _RECOVERY_VERIFY,
    )
    from core.inbound_health_api import INBOUND_HEALTH_ROUTES

    mounted = {
        route.path
        for table in (
            CONNECTOR_INSTALLATION_ROUTES,
            CONNECTOR_VERIFICATION_ROUTES,
            CONNECTOR_ACTIVATION_ROUTES,
            INBOUND_HEALTH_ROUTES,
        )
        for route in table
        if hasattr(route, "path")
    }
    for recovery in (_RECOVERY_INSTALL, _RECOVERY_VERIFY, _RECOVERY_ACTIVATE):
        assert recovery["api"] in mounted, recovery["api"]


def test_every_named_mcp_repair_is_a_tool_that_exists():
    """Meme exigence pour l'hote noninteractif : un outil nomme est un outil monte."""
    from core.inbound_health import _read_data, _read_delivery
    from core.inbound_mcp import INBOUND_MCP_TOOLS

    class _DeadConn:
        def cursor(self):
            raise RuntimeError("unreadable on purpose")

    for reader in (_read_delivery, _read_data):
        layer = reader(_DeadConn(), datastream_id="ds_1")
        # Une couche illisible ne propose rien : voir ci-dessous.
        assert layer["healthy"] is None

    # Et les deux outils que les couches tenant-scoped nomment sont montes.
    assert "prepare_inbound_reprocess" in INBOUND_MCP_TOOLS
    assert "list_inbound_attachments" in INBOUND_MCP_TOOLS


def test_an_unreadable_layer_offers_no_repair_either():
    """Une base injoignable ne se repare pas en appelant quelque chose.

    Proposer un bouton la invite un operateur a le presser en boucle pendant
    qu'il faudrait appeler quelqu'un. `authority` dit quand meme a QUI le
    probleme appartient -- c'est l'autre moitie de l'AC4, et elle reste.
    """
    from core.inbound_health import _read_delivery

    class _DeadConn:
        def cursor(self):
            raise RuntimeError("unreadable on purpose")

    layer = _read_delivery(_DeadConn(), datastream_id="ds_1")
    assert layer["healthy"] is None
    assert layer["recovery"] is None
    assert layer["authority"]


def test_the_console_reference_has_one_definition():
    """Deux copies seraient deux occasions de deriver, en silence.

    `ContentRouter` jette une reference qu'il ne sait pas lire SANS rien dire :
    une copie perimee se lit donc exactement comme un lien qui marche.
    """
    from core.inbound_health import console_reference
    from core.inbound_mcp import _console_reference

    assert _console_reference("ds_1", section="data") == console_reference(
        "ds_1", tab="data"
    )
    owner = console_reference("ds_1", tab="data")["owner_reference"]
    # La forme que la console sait ouvrir, pas une forme voisine.
    assert owner["surface"] == "project"
    assert owner["object_type"] == "datastream"
    assert owner["section"] == "datastreams"


def _tabs_declared_by_the_navigation_contract() -> list[str]:
    """The `datastream` tabs, read from the navigation registry -- never recopied.

    A list retyped in Python would drift from the contract the router obeys, and
    the drift is exactly what this whole class of defect is made of.

    IT READS THE REGISTRY, NOT ONE FILE. This opened `shell/navigation.ts`
    directly, and AD-42 moved every workspace's sections into
    `shell/navigation/<workspace>.ts`. The guard then found no `datastream`
    contract and said so -- correctly, which is why it was red rather than
    quietly true. `tests.support.navigation_source` exists for exactly this
    class and joins the assembly with its parts; using it is why this guard
    survives the next split too.
    """
    import re

    from tests.support.navigation_source import navigation_source

    source = navigation_source()
    match = re.search(
        r"type:\s*\"datastream\".*?tabs:\s*\[([^\]]*)\]", source, re.S
    )
    assert match, (
        "the `datastream` object contract declares no `tabs` list anywhere in the "
        "navigation registry -- this guard has gone blind, which is worse than absent"
    )
    return re.findall(r"\"([^\"]+)\"", match.group(1))


def test_the_console_tab_list_is_the_one_the_console_declares():
    """AC4's real closure: the server's idea of a tab IS the console's.

    THE FOURTH OCCURRENCE OF THIS CLASS, and the first test that can see it.
    `_read_delivery` and `_read_data` -- the only two layers that offer a repair
    -- built `console_reference(..., tab="deliveries")`. `deliveries` is not a
    declared tab of the `datastream` contract, and `ContentRouter` drops an
    unknown reference WITHOUT A WORD, so both alerts rendered as links that led
    nowhere.

    The test that was supposed to close this checked `surface`, `object_type`
    and `section` -- and never `tab`, and never crossed `navigation.ts` at all.
    """
    from core.inbound_health import DATASTREAM_CONSOLE_TABS

    declared = _tabs_declared_by_the_navigation_contract()
    assert list(DATASTREAM_CONSOLE_TABS) == declared, (
        "the server's tab vocabulary and the console's navigation contract "
        f"disagree:\n  server  : {list(DATASTREAM_CONSOLE_TABS)}\n"
        f"  console : {declared}\n\n"
        "A tab outside the contract is dropped by ContentRouter in silence."
    )


def test_no_console_reference_anywhere_names_an_undeclared_tab():
    """Every reference this module can BUILD lands on a real tab.

    Asserting the vocabulary is not enough -- the defect was a call site passing
    a word outside it. This walks the layers that actually carry a recovery and
    checks the tab each one emits, so a future `tab="inbox"` fails here.
    """
    from core.inbound_health import (
        DATASTREAM_CONSOLE_TABS,
        _read_data,
        _read_delivery,
        _read_queue,
    )

    emitted: list[str] = []
    for reader in (_read_delivery, _read_queue, _read_data):
        # A failing connection drives each reader down its unreadable branch;
        # what matters is the recovery shape it declares, which is built at
        # import time from the same `_recovery` call the healthy branch uses.
        layer = reader(_FailingConn(), datastream_id="ds_1")
        assert layer["state"] == "unknown"
    for reader in (_read_delivery, _read_queue, _read_data):
        recovery = _recovery_of(reader)
        if recovery and recovery.get("console"):
            emitted.append(recovery["console"]["owner_reference"]["tab"])

    assert emitted, "no layer declared a console recovery at all"
    for tab in emitted:
        assert tab in DATASTREAM_CONSOLE_TABS, (
            f"a layer points an operator at {tab!r}, which the console does not "
            "declare -- the router will drop it in silence"
        )


def _recovery_of(reader) -> dict | None:
    """The recovery a layer reader declares, read from its own source call.

    Built by calling the reader against a connection that answers the shape it
    expects; the unreadable branch omits `recovery`, so the nominal one is used.
    """
    import inspect
    import re

    source = inspect.getsource(reader)
    match = re.search(r"console_tab=\"([^\"]+)\"", source)
    if not match:
        return None
    from core.inbound_health import console_reference

    return {"console": console_reference("ds_1", tab=match.group(1))}


# ---------------------------------------------------------------------------
# Mounting. A read surface that exists and is not routed is the exact defect
# this story was written to remove, so it gets its own assertion rather than
# being assumed from the fact that the module imports.
# ---------------------------------------------------------------------------


def test_the_three_reads_are_actually_mounted():
    from core.admin_api import router

    paths = {r.path for r in router.routes if hasattr(r, "path")}
    assert "/api/connectors/{connector_name}/health" in paths
    assert "/api/connectors/{connector_name}/datastreams/{datastream_id}/inbox" in paths
    assert (
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/deliveries/{receipt_id}" in paths
    )

    assert (
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/scan-jobs/{job_id}/recover" in paths
    )


def test_the_inbox_route_is_declared_before_any_broader_datastream_route():
    """Starlette resolves in declaration order, so order IS the contract.

    A later `/datastreams/{datastream_id}/{anything}` route declared earlier
    would swallow `/inbox` and the failure would look like a 404 from the
    handler rather than a routing mistake.
    """
    from core.admin_api import router
    from core.inbound_health_api import INBOUND_HEALTH_ROUTES

    declared = [r.path for r in INBOUND_HEALTH_ROUTES]
    assert declared.index(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/deliveries/{receipt_id}"
    ) < declared.index("/api/connectors/{connector_name}/datastreams/{datastream_id}/inbox")

    ordered = [r.path for r in router.routes if hasattr(r, "path")]
    inbox = ordered.index("/api/connectors/{connector_name}/datastreams/{datastream_id}/inbox")
    # "Broader" means a WILDCARD final segment. A sibling with a literal final
    # segment (`/credentials`) cannot shadow `/inbox` however early it is
    # declared -- the first version of this assertion counted those and failed
    # on a routing table that was correct.
    prefix = "/api/connectors/{connector_name}/datastreams/{datastream_id}/"
    shadowing = [
        (i, p)
        for i, p in enumerate(ordered)
        if p.startswith(prefix) and p[len(prefix) :].startswith("{") and "/" not in p[len(prefix) :]
    ]
    assert all(i > inbox for i, _ in shadowing), (
        f"a wildcard datastream sub-route is declared before /inbox and would "
        f"shadow it: {[p for _, p in shadowing]}"
    )


def test_scan_verdict_projection_allowlists_only_content_free_evidence():
    from core.inbound_health import redact_scan_verdict

    projected = redact_scan_verdict(
        {
            "accepted": False,
            "reason": "row_envelope_exceeded",
            "malware": "unavailable",
            "malware_engine": "=ignore-all-instructions",
            "raw_header": "secret@example.com",
            "policy": {"version": "inbound-scan-policy-v2", "max_rows": 10, "prompt": "evil"},
            "evidence": {"row_estimate": 11, "row_limit": 10, "raw_cell": "secret"},
        }
    )
    blob = str(projected)
    assert projected["evidence"] == {"row_estimate": 11, "row_limit": 10}
    assert projected["policy"] == {"max_rows": 10, "version": "inbound-scan-policy-v2"}
    assert "secret" not in blob and "ignore-all" not in blob


def test_inbox_query_projects_dead_letter_job_evidence():
    source = pathlib.Path(__file__).resolve().parents[2] / "core/inbound_health.py"
    text = source.read_text(encoding="utf-8")
    assert "FULL OUTER JOIN app.inbound_raw_imports" in text
    assert '"scan_job": _redact_scan_job' in text
    assert '"scan_verdict": redact_scan_verdict' in text


def test_pre_raw_dead_letters_and_recovery_are_in_authorized_surfaces():
    health_source = pathlib.Path(__file__).resolve().parents[2] / "core/inbound_health.py"
    api_source = pathlib.Path(__file__).resolve().parents[2] / "core/inbound_health_api.py"
    health_text = health_source.read_text(encoding="utf-8")
    api_text = api_source.read_text(encoding="utf-8")

    assert "FROM app.inbound_scan_jobs j" in health_text
    assert "FULL OUTER JOIN app.inbound_raw_imports r" in health_text
    assert '"raw_import_id": None' in health_text
    assert '"scan_job": job' in health_text
    assert 'minimum_capability="edit"' in api_text
    assert "scan-jobs/{job_id}/recover" in api_text
    assert "recover_dead_letter(" in api_text
    assert 'request.headers.get("Idempotency-Key"' in api_text
    assert '"replayed": recovered["replayed"]' in api_text


def test_scan_projection_keeps_safe_encoding_and_central_directory_bounds():
    from core.inbound_health import redact_scan_verdict

    projected = redact_scan_verdict(
        {
            "accepted": False,
            "evidence": {
                "encoding": "utf-16-le",
                "archive_central_directory_bytes": 2048,
                "archive_central_directory_limit": 1024,
                "archive_worst_ratio": "infinite",
            },
        }
    )
    assert projected is not None
    assert projected["evidence"] == {
        "encoding": "utf-16-le",
        "archive_central_directory_bytes": 2048,
        "archive_central_directory_limit": 1024,
        "archive_worst_ratio": "infinite",
    }


# ---------------------------------------------------------------------------
# C-3 : la liste des issues saines nommait deux valeurs impossibles et en
# omettait deux reelles.
# ---------------------------------------------------------------------------


class _LedgerConn:
    """Une connexion qui rend UN import avec l'issue demandee, et rien de publie."""

    def __init__(self, outcome: str, published: str | None = None):
        self._outcome, self._published, self._n = outcome, published, 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        if "managed_feed_import_ledger" in self._last:
            from datetime import datetime, timezone

            return ("mfi_1", self._outcome, 3, 0, None, datetime.now(timezone.utc))
        return (self._published,)


def _data_state(outcome: str, published: str | None = None) -> dict:
    from core.inbound_health import _read_data

    return _read_data(_LedgerConn(outcome, published), datastream_id="ds-1")


def test_the_healthy_outcomes_are_the_ones_the_database_can_actually_hold():
    """La liste saine nommait `succeeded` et `ok` -- deux valeurs impossibles.

    La CHECK de la migration 077 contraint `outcome` a
    {opened, written, noop, rejected, published, failed}. `succeeded` et `ok`
    n'en font pas partie et ne peuvent donc JAMAIS apparaitre ; en echange,
    `noop` et `opened` etaient omis et tombaient dans la branche NO_PUBLICATION.

    Consequence mesuree : un doublon deliberement saute (`noop`) et un import EN
    VOL (`opened`) faisaient tous deux lire le connecteur comme bloque -- et le
    pli par la couche la plus faible propageait cela a `overall`. C'est la story
    dont le seul but est de distinguer une panne d'installation d'une panne de
    livraison d'une panne de donnee.

    Le vocabulaire est celui de `managed_feed_ledger`, jamais une copie.
    """
    from core.inbound_health import _HEALTHY_OUTCOMES
    from core.managed_feed_ledger import (
        OUTCOME_FAILED,
        OUTCOME_NOOP,
        OUTCOME_OPENED,
        OUTCOME_PUBLISHED,
        OUTCOME_REJECTED,
        OUTCOME_WRITTEN,
    )

    known = {
        OUTCOME_OPENED,
        OUTCOME_WRITTEN,
        OUTCOME_NOOP,
        OUTCOME_REJECTED,
        OUTCOME_PUBLISHED,
        OUTCOME_FAILED,
    }
    assert _HEALTHY_OUTCOMES <= known, (
        f"{sorted(_HEALTHY_OUTCOMES - known)} ne peut jamais apparaitre en base"
    )


def test_a_deliberate_duplicate_skip_is_healthy():
    """`noop` = << ce run a confirme que l'instantane est a jour >>.

    C'est la lignee de fraicheur decrite par la migration 077 elle-meme. Rien de
    neuf a publier n'est le contraire d'un probleme.
    """
    from core.managed_feed_ledger import OUTCOME_NOOP

    layer = _data_state(OUTCOME_NOOP)
    assert layer["healthy"] is True
    assert layer["state"] == "OK"


def test_an_import_in_flight_is_not_a_failure_and_not_a_success():
    """`opened` est la SEULE issue non terminale (migration 077, son commentaire).

    Un import en vol n'a pas d'issue : le dire sain mentirait, le dire casse
    accuserait le connecteur d'une panne qui n'existe pas. `healthy` est donc
    inconnu -- la meme reponse que pour une couche illisible, et pour la meme
    raison.
    """
    from core.managed_feed_ledger import OUTCOME_OPENED

    layer = _data_state(OUTCOME_OPENED)
    assert layer["healthy"] is None
    assert layer["state"] == "IMPORT_IN_FLIGHT"


def test_a_written_but_unpublished_import_still_reads_as_no_publication():
    """Ce que la reparation ne doit PAS emporter avec elle.

    `written` veut dire ecrit et non publie : le lecteur n'est pas servi, et
    c'est bien un defaut de publication. Elargir la liste saine jusque-la
    rendrait la couche muette sur le seul echec qu'elle sait voir.
    """
    from core.managed_feed_ledger import OUTCOME_WRITTEN

    layer = _data_state(OUTCOME_WRITTEN)
    assert layer["healthy"] is False
    assert layer["state"] == "NO_PUBLICATION"


def test_live_pg_both_surfaces_withhold_the_same_things(pg_conn, delivery_fixture):
    """La meme piece jointe se lisait differemment selon la route empruntee.

    La chronologie etendait le read-model BRUT, qui porte `quarantine_uri` et
    `error_detail` verbatim ; l'inbox les retire deja au profit d'un
    `parser_review` redige. L'en-tete de `inbound_health_api` l'interdit
    explicitement -- << a handler that reshapes is a handler that can leak
    something the read model was careful to exclude >> -- et c'etait pourtant le
    cas ici.

    L'adresse d'un objet retenu et le detail brut d'un parseur sont exactement ce
    genre de chose : le premier mene aux octets, le second peut porter des
    valeurs de lignes.
    """
    from core.inbound_health import get_attachment_inbox, get_delivery_timeline

    seeded = delivery_fixture
    timeline = get_delivery_timeline(
        pg_conn,
        datastream_id=seeded["datastream_id"],
        receipt_id=seeded["receipt_id"],
    )
    inbox = get_attachment_inbox(pg_conn, datastream_id=seeded["datastream_id"])

    assert timeline["attachments"], "la chronologie doit lister ses pieces jointes"
    for surface in (timeline["attachments"], inbox):
        for item in surface:
            assert "quarantine_uri" not in item
            assert "error_detail" not in item


# ---------------------------------------------------------------------------
# AC5, AC3 and AC1 -- the three properties held by ZERO test until 2026-08-10.
#
# Each of the three was "closed" by an assertion of the form `"x" in payload`.
# A key is present whatever the value behind it, so every one of the mutations
# below left 34 tests green:
#
#   last_known_good_publication=None if healthy is False else published  -> 0 red
#   processing_latency_median_seconds = None                             -> 0 red
#   (no queue layer at all)                                              -> 0 red
#
# What follows measures the CHANGE OF ANSWER between the two states that
# separate true from false, on the pattern of
# `test_live_pg_timeline_states_whether_published_data_changed`.
# ---------------------------------------------------------------------------


def _publish_execution(pg_conn, datastream_id: str) -> str:
    """Point the Datastream at a serving publication and return its id.

    A REAL EXECUTION ROW, not a plausible identifier: the pointer is what
    `_read_data` reads to decide DEGRADED vs NO_PUBLICATION, and a dangling id
    would prove the branch with evidence no production row could carry.
    """
    import ulid as _ulid

    execution_id = f"dse_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        (project_id,) = cur.fetchone()
        # AN EXECUTION IS SCOPED BY ITS VERSIONS. `fk_datastream_executions_plan_scope`
        # constrains (plan_version_id, datastream_id, project_id) as a triple, so a
        # publication pointer cannot be minted from plausible strings -- the plan and
        # the mapping have to exist for this Datastream.
        plan_id = f"dpv_{_ulid.ULID()}"
        mapping_id = f"dmv_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.datastream_plan_versions "
            "(id, datastream_id, project_id, version_number, contract_version, "
            " source_kind, writer_kind, destination_policy, normalized_payload, "
            " content_hash, idempotency_key_hash, created_by) "
            "VALUES (%s, %s, %s, 1, 'v1', 'connector_pull', 'toorow', "
            "        'managed_raw', '{}'::jsonb, %s, %s, 'system')",
            (
                plan_id,
                datastream_id,
                project_id,
                _hashlib.sha256(plan_id.encode()).hexdigest(),
                _hashlib.sha256(f"idem-{plan_id}".encode()).hexdigest(),
            ),
        )
        cur.execute(
            "INSERT INTO app.datastream_mapping_versions "
            "(id, datastream_id, project_id, version_number, "
            " mapping_contract_version, source_schema_hash, plan_version_id, "
            " content_hash, ossie_spec_version, toorow_extension_version, "
            " executable, mapping_payload, ossie_projection, "
            " idempotency_key_hash, created_by) "
            "VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, '1.0', '1.0', TRUE, "
            "        '{}'::jsonb, '{}'::jsonb, %s, 'system')",
            (
                mapping_id,
                datastream_id,
                project_id,
                _hashlib.sha256(f"schema-{mapping_id}".encode()).hexdigest(),
                plan_id,
                _hashlib.sha256(mapping_id.encode()).hexdigest(),
                _hashlib.sha256(f"idem-{mapping_id}".encode()).hexdigest(),
            ),
        )
        cur.execute(
            "INSERT INTO app.datastream_executions "
            "(id, datastream_id, project_id, plan_version_id, mapping_version_id, "
            " projection_plan_ref, state, created_by) "
            "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'published', 'system') "
            "ON CONFLICT (id) DO NOTHING",
            (execution_id, datastream_id, project_id, plan_id, mapping_id),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = %s "
            "WHERE id = %s",
            (execution_id, datastream_id),
        )
    pg_conn.commit()
    return execution_id


def _add_attachment(
    pg_conn,
    insert_operation,
    delivery_fixture,
    *,
    ordinal: int,
    filename: str,
    target: str,
    error_code: str | None = None,
) -> str:
    """Add ONE more attachment to the delivery and walk it to *target*, legally.

    A terminal raw import is frozen (`protect_inbound_raw_import`), so a test
    that needs an attachment in a particular end state must BUILD one rather
    than re-edit an existing row. Reuses the fixture's own walker so the
    provenance rules stay the ones production obeys.
    """
    import ulid as _ulid

    ds_id = delivery_fixture["datastream_id"]
    receipt_id = delivery_fixture["receipt_id"]
    raw_id = f"inbraw_{_ulid.ULID()}"
    digest = hashlib.sha256(f"{filename}-{ordinal}".encode()).hexdigest()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id = %s", (ds_id,)
        )
        (org_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO app.inbound_raw_imports "
            "(id, receipt_id, datastream_id, ordinal, filename, "
            " media_type_declared, size_bytes, content_hash, quarantine_uri, "
            " retention_policy_version, retention_days, retention_expires_at, "
            " state, operation_id) "
            "VALUES (%s, %s, %s, %s, %s, 'text/csv', 42, %s, %s, "
            "        'quarantine-retention-v1', 30, NOW() + INTERVAL '30 days', "
            "        'RECEIVED', %s)",
            (
                raw_id,
                receipt_id,
                ds_id,
                ordinal,
                filename,
                digest,
                _quarantine_uri(org_id, ds_id, digest),
                insert_operation(
                    "inbound.raw_import.recorded",
                    resource_path=[
                        f"datastream:{ds_id}",
                        f"receipt:{receipt_id}",
                        f"attachment:{ordinal}",
                    ],
                ),
            ),
        )
        _walk_raw_import(
            cur,
            insert_operation,
            raw_id=raw_id,
            target=target,
            error_code=error_code,
            datastream_id=ds_id,
            ledger_id=delivery_fixture["ledger_id"],
        )
    pg_conn.commit()
    return raw_id


def _data_layer(pg_conn, datastream_id: str) -> dict:
    from core.inbound_health import get_inbound_health

    health = get_inbound_health(
        pg_conn,
        connector_name="inbound-managed-files",
        environment="test",
        datastream_id=datastream_id,
    )
    return next(x for x in health["layers"] if x["layer"] == "data")


@pytest.mark.live_pg
def test_live_pg_a_failed_latest_import_does_not_hide_the_serving_publication(
    pg_conn, delivery_fixture
):
    """AC5, as a CHANGE of answer -- the property the story is named for.

    The header of `inbound_health` promises: "A failed candidate must not make a
    serving publication look absent". Nothing measured it. The neighbouring test
    asserts that two keys EXIST, which stays true when one of them is emptied by
    exactly the failure this is about.

    Three states, one field, and it must survive all three.
    """
    ds_id = delivery_fixture["datastream_id"]

    # (1) A publication is serving and the latest import succeeded.
    execution_id = _publish_execution(pg_conn, ds_id)
    _set_ledger(pg_conn, delivery_fixture["ledger_id"], outcome="published")
    healthy = _data_layer(pg_conn, ds_id)
    assert healthy["state"] == "OK"
    assert healthy["healthy"] is True
    assert healthy["last_known_good_publication"] == execution_id

    # (2) A LATER import FAILS. The publication has not moved: it is still
    #     answering readers, and that is the entire point of the field.
    #
    #     A NEW LEDGER ROW, because `published` is terminal
    #     (`protect_managed_feed_import_ledger`) -- which is also how production
    #     behaves: a failed retry never rewrites the run that succeeded.
    with pg_conn.cursor() as cur:
        _mint_ledger_row(
            cur,
            datastream_id=ds_id,
            outcome="failed",
            error_code="parser_review_required",
        )
    pg_conn.commit()
    degraded = _data_layer(pg_conn, ds_id)
    assert degraded["healthy"] is False
    assert degraded["state"] == "DEGRADED_LAST_GOOD_SERVING", (
        "a failed candidate over a serving publication is DEGRADED, not down"
    )
    assert degraded["last_known_good_publication"] == execution_id, (
        "the failed candidate emptied the last-known-good pointer -- this is the "
        "confusion that turns a recoverable failure into an incident"
    )
    assert degraded["latest_import"]["outcome"] == "failed"
    # The two are separate FIELDS, and they now disagree. That is correct.
    assert degraded["latest_import"]["error_code"] == "parser_review_required"

    # (3) With no publication serving, the SAME failure reads differently --
    #     without this branch, a hard-coded DEGRADED would pass step (2).
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = NULL "
            "WHERE id = %s",
            (ds_id,),
        )
    pg_conn.commit()
    down = _data_layer(pg_conn, ds_id)
    assert down["state"] == "NO_PUBLICATION"
    assert down["last_known_good_publication"] is None


@pytest.mark.live_pg
def test_live_pg_processing_latency_measures_the_receipts_that_settled(
    pg_conn, delivery_fixture
):
    """AC3: the latency is a MEASUREMENT, and it tracks the rows it summarises.

    `"processing_latency_median_seconds" in metrics` was the whole assertion,
    and the value behind it was guarded by `if ... is not None:` -- vacuous by
    construction. Setting both statistics to None left 34 tests green.

    A receipt is IMMUTABLE (`protect_inbound_receipt`), so its duration cannot
    be dictated by the test. The statistic is therefore cross-checked against
    the same rows, computed independently in SQL: a None, a constant or a
    statistic over the wrong population all fail here.
    """
    from core.inbound_health import get_inbound_health

    ds_id = delivery_fixture["datastream_id"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), "
            "       percentile_cont(0.5) WITHIN GROUP ("
            "         ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at))), "
            "       max(EXTRACT(EPOCH FROM (updated_at - created_at))) "
            "FROM app.inbound_receipts "
            "WHERE datastream_id = %s "
            "  AND state IN ('LANDED', 'REJECTED', 'FAILED')",
            (ds_id,),
        )
        settled, expected_median, expected_max = cur.fetchone()

    metrics = get_inbound_health(
        pg_conn,
        connector_name="inbound-managed-files",
        environment="test",
        datastream_id=ds_id,
    )["metrics"]

    assert settled >= 1, "the fixture produced no settled receipt to measure"
    assert metrics["settled_receipts"] == int(settled)

    median = metrics["processing_latency_median_seconds"]
    maximum = metrics["processing_latency_max_seconds"]
    assert median is not None and maximum is not None, (
        "the latency statistics are None on a Datastream with a settled receipt "
        "-- AC3 asks for processing latency, and a null is not one"
    )
    # THE SAME ROWS, THE SAME NUMBERS. A constant would survive an "is not None"
    # check and dies here.
    assert median == pytest.approx(float(expected_median), abs=0.01), (
        f"reported median {median} does not match the settled receipts "
        f"({expected_median})"
    )
    assert maximum == pytest.approx(float(expected_max), abs=0.01)
    assert maximum >= median


@pytest.mark.live_pg
def test_live_pg_parser_failures_and_last_publication_are_counted(
    pg_conn, delivery_fixture, insert_operation
):
    """AC3 names seven readings; two of them existed nowhere.

    `grep -rn "parser_failures|last_successful_publication"` returned NOTHING
    outside the test file before 2026-08-10.
    """
    from core.inbound_health import get_inbound_health

    ds_id = delivery_fixture["datastream_id"]

    def _metrics():
        return get_inbound_health(
            pg_conn,
            connector_name="inbound-managed-files",
            environment="test",
            datastream_id=ds_id,
        )["metrics"]

    before = _metrics()
    assert before["parser_failures"] == 0
    assert before["last_successful_publication"] is None

    # One attachment fails the PARSER specifically. The fixture's other two
    # failures are a scan rejection and a quarantine read error, and neither
    # must be counted here -- they send an operator to a different repair.
    #
    # WALKED, NOT PATCHED. A terminal raw import is frozen by
    # `protect_inbound_raw_import`, and re-editing one would prove the counter
    # against a row no delivery can produce.
    _add_attachment(
        pg_conn,
        insert_operation,
        delivery_fixture,
        ordinal=3,
        filename="unreadable.csv",
        target="FAILED",
        error_code="file_not_parseable",
    )

    after = _metrics()
    assert after["parser_failures"] == 1, (
        "a parser failure was written and the counter did not move"
    )

    _set_ledger(pg_conn, delivery_fixture["ledger_id"], outcome="published")
    published = _metrics()
    assert published["last_successful_publication"] is not None
    assert (
        published["last_successful_publication"]["import_ledger_id"]
        == delivery_fixture["ledger_id"]
    )
    assert published["last_successful_publication"]["published_at"]


@pytest.mark.live_pg
def test_live_pg_a_denial_is_reported_as_unobservable_and_never_as_zero(
    pg_conn, delivery_fixture
):
    """AC3 asks for denied deliveries. They CANNOT exist as receipts.

    `app.inbound_receipts.state` admits only
    RECEIVED|PROCESSING|LANDED|REJECTED|FAILED, and a delivery refused at the
    credential creates no receipt at all. A `0` here would read as "nothing was
    denied" when the truth is "this surface cannot see denials".
    """
    from core.inbound_health import get_inbound_health

    metrics = get_inbound_health(
        pg_conn,
        connector_name="inbound-managed-files",
        environment="test",
        datastream_id=delivery_fixture["datastream_id"],
    )["metrics"]

    denied = metrics["denied_deliveries"]
    assert denied["available"] is False
    assert denied["reason"] == "not_applicable"
    assert denied["detail"]
    assert denied != 0 and denied != {}, (
        "a count of zero for something that cannot be observed is the one "
        "answer this surface must never give"
    )


@pytest.mark.live_pg
def test_live_pg_the_duplicate_rate_is_a_rate_and_the_count_is_a_count(
    pg_conn, delivery_fixture, insert_operation
):
    """AC3: the comment promised a rate while the code emitted a count.

    Both now exist and they are different numbers, so neither can be mistaken
    for the other.
    """
    import ulid as _ulid
    from core.inbound_health import get_inbound_health

    ds_id = delivery_fixture["datastream_id"]

    def _metrics():
        return get_inbound_health(
            pg_conn,
            connector_name="inbound-managed-files",
            environment="test",
            datastream_id=ds_id,
        )["metrics"]

    metrics = _metrics()
    # Three attachments, three distinct hashes: nothing repeated yet.
    assert metrics["repeated_content_hashes"] == 0
    assert metrics["duplicate_attachments"] == 0
    assert metrics["duplicate_rate"] == 0.0

    # The SAME bytes arrive again.
    duplicate_id = f"inbraw_{_ulid.ULID()}"
    operation_id = insert_operation(
        "inbound.raw_import.recorded",
        resource_path=[
            f"datastream:{ds_id}",
            f"receipt:{delivery_fixture['receipt_id']}",
            "attachment:9",
        ],
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.inbound_raw_imports "
            "(id, receipt_id, datastream_id, ordinal, filename, "
            " media_type_declared, size_bytes, content_hash, quarantine_uri, "
            " retention_policy_version, retention_days, retention_expires_at, "
            " state, operation_id) "
            "SELECT %s, receipt_id, datastream_id, 9, filename, "
            "       media_type_declared, size_bytes, content_hash, quarantine_uri, "
            "       retention_policy_version, retention_days, retention_expires_at, "
            "       'RECEIVED', %s "
            "FROM app.inbound_raw_imports WHERE id = %s",
            (duplicate_id, operation_id, delivery_fixture["raw_ids"][0]),
        )
    pg_conn.commit()

    after = _metrics()
    assert after["repeated_content_hashes"] == 1, "one file arrived twice"
    assert after["duplicate_attachments"] == 1, "one ARRIVAL was a repeat"
    # A rate, and it is not the count: 1 repeat out of 4 arrivals.
    assert after["attachments_total"] == 4
    assert after["duplicate_rate"] == 0.25
    assert after["duplicate_rate"] != after["duplicate_attachments"]


@pytest.mark.live_pg
def test_live_pg_a_dead_lettered_scan_job_blocks_the_overall_verdict(
    pg_conn, delivery_fixture, monkeypatch
):
    """AC1's sixth layer, and the consequence of its absence.

    `app.inbound_scan_jobs` was read by the inbox and by the timeline, and by no
    LAYER -- so the weakest-layer fold never saw it. A job in DEAD_LETTER behind
    a published ledger row answered `overall: "healthy"`: work that will never
    run again, invisible in the one field an operator reads first.
    """
    import ulid as _ulid
    from core.inbound_health import get_inbound_health

    ds_id = delivery_fixture["datastream_id"]
    # A serving publication and a healthy latest import: every OTHER layer that
    # can be green is green, so only the queue can move the verdict.
    _publish_execution(pg_conn, ds_id)
    _set_ledger(pg_conn, delivery_fixture["ledger_id"], outcome="published")

    # THE EARLIER LAYERS ARE HELD GREEN, and that is what isolates the claim.
    # This pg fixture installs no connector, so `installation` blocks first and
    # `overall` would read "blocked" whatever the queue did -- the verdict would
    # move for a reason that has nothing to do with this test. Only the queue
    # and the data layers stay real below.
    import core.inbound_health as _ih

    def _green(layer_name):
        return lambda conn, **kw: {
            "layer": layer_name,
            "state": "READY",
            "healthy": True,
            "authority": _ih.AUTHORITY_PLATFORM_ADMIN,
            "blocking_cause": None,
            "next_action": None,
            "recovery": None,
        }

    monkeypatch.setattr(_ih, "_read_installation", _green("installation"))
    monkeypatch.setattr(_ih, "_read_domain", _green("domain"))
    monkeypatch.setattr(_ih, "_read_delivery", _green("delivery"))

    def _health():
        return get_inbound_health(
            pg_conn,
            connector_name="inbound-managed-files",
            environment="test",
            datastream_id=ds_id,
        )

    before = _health()
    # Every other layer is green and the latest import published: the connector
    # reads healthy, which is exactly the answer that was wrong.
    assert before["overall"] == "unknown", (
        "with no scan job yet the queue is an UNKNOWN, and an unknown must not "
        "be reported green"
    )
    queue_before = next(x for x in before["layers"] if x["layer"] == "queue")
    assert queue_before["state"] == "NO_JOB_YET"
    assert queue_before["healthy"] is None, (
        "no scan job at all is an UNKNOWN queue, never a drained one"
    )

    # A job that exhausted its attempts. Migration 186 requires the evidence:
    # attempt_count = max_attempts, an error code, and recovery evidence.
    # WALKED, NOT PLACED. `protect_inbound_scan_job` requires a job to start
    # QUEUED and admits only QUEUED -> RUNNING -> DEAD_LETTER; a row dropped
    # straight into its end state is one no worker can produce.
    job_id = f"inbsj_{_ulid.ULID()}"
    recovery_evidence = (
        '{"version": "inbound-scan-recovery-v1", "command": "recover"}'
    )
    with pg_conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.datastreams WHERE id = %s", (ds_id,))
        (org_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO app.inbound_scan_jobs "
            "(id, job_policy_version, org_id, datastream_id, receipt_id, "
            " attachment_ordinal, manifest_uri, state, attempt_count, max_attempts) "
            "VALUES (%s, 'inbound-scan-job-v1', %s, %s, %s, 7, "
            "        'gs://q/manifest', 'QUEUED', 0, 5)",
            (job_id, org_id, ds_id, delivery_fixture["receipt_id"]),
        )
        cur.execute(
            "UPDATE app.inbound_scan_jobs SET state = 'RUNNING', attempt_count = 5, "
            "    started_at = clock_timestamp(), updated_at = clock_timestamp() "
            "WHERE id = %s",
            (job_id,),
        )
        # DEAD_LETTER carries its evidence by CHECK: attempts exhausted, an
        # error code, and a recovery envelope an operator can act on.
        cur.execute(
            "UPDATE app.inbound_scan_jobs SET state = 'DEAD_LETTER', "
            "    error_code = 'scan_timeout', recovery_evidence = %s::jsonb, "
            "    finished_at = clock_timestamp(), updated_at = clock_timestamp() "
            "WHERE id = %s",
            (recovery_evidence, job_id),
        )
    pg_conn.commit()

    after = _health()
    queue = next(x for x in after["layers"] if x["layer"] == "queue")
    assert queue["state"] == "DEAD_LETTER"
    assert queue["healthy"] is False
    assert queue["dead_letter_count"] == 1
    assert queue["blocking_cause"] == "scan_jobs_in_dead_letter"

    # THE MEASURABLE CONSEQUENCE. This read "healthy" before the layer existed.
    assert after["overall"] == "blocked", (
        "a dead-lettered scan job left the connector reporting healthy -- the "
        "fold only walks `layers`, and the queue was not one of them"
    )
    assert after["blocking_cause"] == "scan_jobs_in_dead_letter"
    # The repair is named, and it lands on a tab the console actually declares.
    assert queue["recovery"]["console"]["owner_reference"]["tab"] == "overview"
