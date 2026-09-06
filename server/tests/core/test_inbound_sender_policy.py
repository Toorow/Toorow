"""Story 57.3 -- the declared sender allowlist, and where it is allowed to bite.

Three things are pinned here, and each was a decision rather than a detail:

  (a) An UNDECLARED list changes nothing. The token carried in the address stays
      the only authorization there has ever been; a Datastream that declares no
      sender receives exactly what it received before.
  (b) A declared list is matched on HASHES. The manifest lives in the quarantine
      store beside the delivered bytes, and `inbound/receipt.py` states its own
      rule -- the raw routing token and the raw recipient are never written, only
      their sha256. The sender follows it.
  (c) A declared list FAILS CLOSED on a delivery with no sender. An operator who
      asked for a restriction that cannot be checked must not get it silently
      waived; that is how a control becomes decoration.

Offline: pure functions plus one config read against a scripted cursor. No
provider, no account, no delivery.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


class _ConfigCursor:
    def __init__(self, config):
        self._config = config

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        assert "app.datastreams" in sql

    def fetchone(self):
        return None if self._config is None else (self._config,)


class _Conn:
    def __init__(self, config):
        self._cursor = _ConfigCursor(config)

    def cursor(self):
        return self._cursor


def test_no_declared_list_lets_every_delivery_through() -> None:
    from core.inbound_sender_policy import delivery_is_allowed

    assert delivery_is_allowed(allowed=[], sender_hash=None, sender_domain_hash=None)
    assert delivery_is_allowed(
        allowed=[], sender_hash=_digest("anyone@agency.invalid"), sender_domain_hash=None
    )


def test_a_declared_address_matches_only_that_address() -> None:
    from core.inbound_sender_policy import delivery_is_allowed

    allowed = ["reports@agency.invalid"]
    assert delivery_is_allowed(
        allowed=allowed,
        sender_hash=_digest("Reports@Agency.invalid"),
        sender_domain_hash=_digest("agency.invalid"),
    )
    assert not delivery_is_allowed(
        allowed=allowed,
        sender_hash=_digest("someone-else@agency.invalid"),
        sender_domain_hash=_digest("agency.invalid"),
    )


def test_a_declared_domain_matches_every_address_of_that_domain() -> None:
    """A whole domain is allowed without any address being written down."""
    from core.inbound_sender_policy import delivery_is_allowed

    assert delivery_is_allowed(
        allowed=["agency.invalid"],
        sender_hash=_digest("anyone@agency.invalid"),
        sender_domain_hash=_digest("agency.invalid"),
    )
    assert not delivery_is_allowed(
        allowed=["agency.invalid"],
        sender_hash=_digest("anyone@other.invalid"),
        sender_domain_hash=_digest("other.invalid"),
    )


def test_a_declared_list_fails_closed_when_the_delivery_carries_no_sender() -> None:
    """A webhook has no sender. A restriction that cannot be checked refuses."""
    from core.inbound_sender_policy import delivery_is_allowed

    assert not delivery_is_allowed(
        allowed=["agency.invalid"], sender_hash=None, sender_domain_hash=None
    )


def test_the_fingerprints_never_carry_the_address_itself() -> None:
    from core.inbound_sender_policy import sender_fingerprints

    prints = sender_fingerprints("  Reports@Agency.invalid ")
    assert prints == {
        "sender_hash": _digest("reports@agency.invalid"),
        "sender_domain_hash": _digest("agency.invalid"),
    }
    assert "agency" not in str(prints)
    assert sender_fingerprints("") is None
    assert sender_fingerprints(None) is None
    assert sender_fingerprints("not-an-address") is None


def test_the_list_is_read_from_this_datastream_and_nowhere_else() -> None:
    from core.inbound_sender_policy import read_allowed_senders

    config = {"channel_contract": {"allowed_senders": ["Reports@Agency.invalid"]}}
    assert read_allowed_senders(_Conn(config), datastream_id="ds_1") == [
        "reports@agency.invalid"
    ]
    assert read_allowed_senders(_Conn(json.dumps(config)), datastream_id="ds_1") == [
        "reports@agency.invalid"
    ]
    assert read_allowed_senders(_Conn({"source_owner": {}}), datastream_id="ds_1") == []
    assert read_allowed_senders(_Conn(None), datastream_id="ds_1") == []


# EVERY STATEMENT `ingest_inbound_file` ISSUES ON THE WAY TO -- AND ONE STEP
# PAST -- THE SENDER GUARD, NAMED ONCE (AI-317). The `if/elif` this replaces
# ended in `else: self._answer = None`, and on this path `None` is never a
# refusal: every read helper of `inbound_ingest` treats it as a FACT. That
# silence hid the third statement below entirely -- the fake was described as
# answering "the two reads the guard's neighbourhood performs" while the two
# tests that walk PAST the guard issue a third one it had never been shown.
#
# Declaration order is the order an `if/elif` would test in: first match wins.
# The two reads of `app.inbound_raw_imports` are told apart by their PROJECTION,
# not by their relation, because both name the same table.
_STATEMENTS = StatementInventory(
    "test_inbound_sender_policy._IngestCursor",
    # inbound_ingest.py:117 -- `_load_ingestable_datastream`: the five columns
    # every unattended precondition is asserted from, this file's `config`
    # among them.
    ingestable_datastream="select source_kind, enabled, config",
    # inbound_sender_policy.py:115 -- `read_delivery_sender`: the sender digests
    # recorded on the receipt these bytes arrived under. The only durable
    # evidence a replay can read, and the reason this file exists.
    delivery_sender="rx.sender_hash",
    # inbound_ingest.py:807 -- `_load_frozen_dispatch_bundle`, issued the moment
    # a delivery gets PAST the guard. NEVER MODELLED: it joins `app.datastreams`
    # rather than selecting from it, so it matched no branch and was answered
    # "no row" by the `else`. Answering it is what the two passing tests below
    # actually prove -- that the refusal did not fire, and that the next link
    # failed for its own reason.
    frozen_dispatch_bundle="r.dispatch_bundle, r.dispatch_bundle_fingerprint",
)


class _IngestCursor:
    """Answers the reads `ingest_inbound_file` performs, and REFUSES the rest.

    `description` is DERIVED from the statement rather than spelled out: a
    hand-written column tuple is a second copy of the projection, and it is the
    copy that goes stale first, because nothing reads it.
    """

    def __init__(self, *, config: dict, sender: tuple[str | None, str | None]):
        self._config = config
        self._sender = sender
        self._answer = None
        self.description = None
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        statement = _STATEMENTS.match(sql)
        self.description = describe(sql)
        match statement:
            case "ingestable_datastream":
                self._answer = ("managed_feed", True, self._config, "pv_1", "mv_1")
            case "delivery_sender":
                self._answer = self._sender
            case "frozen_dispatch_bundle":
                # NO FROZEN BUNDLE, honestly: this fixture stands up a delivery
                # and nothing else, so the raw import carries none. The product
                # reads that as "accepted raw import has no frozen governed
                # dispatch bundle" and refuses -- which is the pipeline failing
                # for its own reason, one link past the guard.
                self._answer = None
            case _:  # pragma: no cover - a name in the inventory, unanswered
                raise _STATEMENTS.unknown(sql)

    def fetchone(self):
        return self._answer


def _ingest(config: dict, sender: tuple[str | None, str | None], raw_import_id="inbraw_1"):
    """Drive the REAL `ingest_inbound_file` as far as the sender guard."""
    from core.inbound_ingest import ingest_inbound_file

    cursor = _IngestCursor(config=config, sender=sender)
    return ingest_inbound_file(
        _RowConn(cursor),
        datastream_id="ds_1",
        project_id="proj_1",
        file_bytes=b"date,spend\n2026-01-01,1\n",
        filename="report.csv",
        channel="email",
        message_id="msg_1",
        actor="inbound-worker",
        raw_import_id=raw_import_id,
    )


class _RowConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


_DECLARED = {
    "channels": ["email"],
    "channel_contract": {"allowed_senders": ["reports@agency.invalid"]},
}


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The chain this replaces ended in `else: self._answer = None`, and nothing on
    this path reads `None` as a gap -- `_load_ingestable_datastream` reads it as
    "no such Datastream", `_load_frozen_dispatch_bundle` as "no frozen bundle",
    `read_delivery_sender` as "no recorded sender", which against a declared
    list is a REFUSAL. A read added to the inbound path tomorrow would have been
    answered in silence, and the refusals asserted below would have been about a
    sender the guard never actually looked up.
    """
    cursor = _IngestCursor(config=_DECLARED, sender=(None, None))
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT allow_empty_publication FROM app.project_preferences "
            "WHERE project_id = %s"
        )
    message = str(raised.value)
    # The statement that moved, and a neighbour to compare it against.
    assert "app.project_preferences" in message
    assert "delivery_sender" in message

    # The two reads of `app.inbound_raw_imports` really are told apart.
    assert (
        _STATEMENTS.find(
            "SELECT rx.sender_hash, rx.sender_domain_hash "
            "FROM app.inbound_raw_imports r "
            "JOIN app.inbound_receipts rx ON rx.id = r.receipt_id WHERE r.id = %s"
        )
        == "delivery_sender"
    )
    assert (
        _STATEMENTS.find(
            "SELECT r.dispatch_bundle, r.dispatch_bundle_fingerprint "
            "FROM app.inbound_raw_imports r "
            "JOIN app.datastreams d ON d.id = r.datastream_id AND d.project_id = %s "
            "WHERE r.id = %s AND r.datastream_id = %s"
        )
        == "frozen_dispatch_bundle"
    )


def test_the_guard_lives_where_both_import_paths_meet() -> None:
    """`ingest_inbound_file` is the join, and the refusal is named there.

    Both callers reach this function: a delivery arriving
    (`inbound_processing._process_one_attachment`) and a retained delivery being
    replayed (`inbound_reprocess.execute_reprocess`). Guarding either branch
    instead of the point they share is what left the replay unchecked.
    """
    from core.inbound_ingest import DatastreamNotIngestable, SenderNotAllowed

    with pytest.raises(SenderNotAllowed) as refused:
        _ingest(_DECLARED, (_digest("stranger@elsewhere.invalid"), _digest("elsewhere.invalid")))

    # A subclass, so every caller already catching the base class refuses it too.
    assert isinstance(refused.value, DatastreamNotIngestable)
    assert refused.value.code == "sender_not_allowed"
    # The refusal is the answer; the address is the tenant's data and this string
    # reaches logs and receipts.
    assert "stranger" not in str(refused.value)
    assert "elsewhere.invalid" not in str(refused.value)


def test_a_replay_of_a_delivery_received_before_the_list_is_refused_too() -> None:
    """THE CASE THE LIST EXISTS FOR, and the one the first version let through.

    A list is declared after seeing an arrival one did not want. The unwanted
    file is already retained, so the only thing left to stop is its replay --
    which reads its sender from the receipt, the sole durable evidence a replay
    has. Same `raw_import_id` the reprocess passes, same guard, same refusal.
    """
    from core.inbound_ingest import SenderNotAllowed

    with pytest.raises(SenderNotAllowed):
        _ingest(_DECLARED, (_digest("stranger@elsewhere.invalid"), None))


def test_a_delivery_with_no_recorded_sender_is_refused_by_a_declared_list() -> None:
    """FAIL CLOSED. A receipt written before migration 214 records no sender.

    Accepting it would make every pre-existing delivery a way past the list.
    """
    from core.inbound_ingest import SenderNotAllowed

    with pytest.raises(SenderNotAllowed):
        _ingest(_DECLARED, (None, None))
    with pytest.raises(SenderNotAllowed):
        _ingest(_DECLARED, (None, None), raw_import_id=None)


def test_a_declared_sender_that_matches_passes_the_guard() -> None:
    """It must let the right file through, or it is not a guard but a wall."""
    from core.inbound_ingest import SenderNotAllowed

    try:
        _ingest(_DECLARED, (_digest("reports@agency.invalid"), _digest("agency.invalid")))
    except SenderNotAllowed:  # pragma: no cover -- the assertion IS the raise
        raise AssertionError("a declared sender was refused by its own allowlist") from None
    except UnknownStatement:
        # NOT swallowed by the catch-all below. `UnknownStatement` is an
        # `AssertionError`, hence an `Exception`, and a blanket `pass` here would
        # hand the fake's silence straight back after we spent the work removing
        # it -- the test would go green on a statement nobody modelled.
        raise
    except Exception:
        # Past the guard: what fails next is the rest of the pipeline, which this
        # test does not stand up. Reaching it is the proof.
        pass


def test_an_undeclared_list_never_reads_a_sender_at_all() -> None:
    """No declaration, no lookup, no behaviour change for every other feed."""
    from core.inbound_ingest import SenderNotAllowed

    cursor = _IngestCursor(config={"channels": ["email"]}, sender=(None, None))
    from core.inbound_ingest import ingest_inbound_file

    try:
        ingest_inbound_file(
            _RowConn(cursor),
            datastream_id="ds_1",
            project_id="proj_1",
            file_bytes=b"date,spend\n2026-01-01,1\n",
            filename="report.csv",
            channel="email",
            message_id="msg_1",
            actor="inbound-worker",
            raw_import_id="inbraw_1",
        )
    except SenderNotAllowed:  # pragma: no cover
        raise AssertionError("a feed that declares no sender was refused") from None
    except UnknownStatement:  # see the note in the test above
        raise
    except Exception:
        pass
    assert not [sql for sql in cursor.statements if "rx.sender_hash" in sql]


def test_the_receipt_answer_stays_constant_whatever_the_sender() -> None:
    """The 403 must never vary: it would enumerate which addresses exist.

    Read in the source of the internet-facing handler, because that is where the
    temptation to answer "unknown sender" lives. The allowlist is applied in
    `core.inbound_processing`, on a durable receipt, and nowhere earlier.
    """
    from pathlib import Path

    receipt = (
        Path(__file__).resolve().parents[2] / "inbound" / "receipt.py"
    ).read_text(encoding="utf-8")
    processing = (
        Path(__file__).resolve().parents[2] / "core" / "inbound_processing.py"
    ).read_text(encoding="utf-8")

    assert "allowed_senders" not in receipt, (
        "la liste blanche ne se lit pas au recu : sa reponse doit rester constante"
    )
    assert "_sender_digests(" in receipt, "le recu ne hache plus l'expediteur"
    # L'expediteur hache est PERSISTE au recu par le chemin d'arrivee -- c'est la
    # seule preuve durable que le rejeu pourra lire.
    assert "sender_hash=manifest.get" in processing, (
        "l'expediteur n'atteint plus le recu ; le rejeu n'aurait rien a lire"
    )
    # Et le REFUS ne vit pas ici : il vit au point que les deux chemins d'import
    # partagent. Une liste blanche gardee dans une seule branche n'en est pas une.
    ingest = (
        Path(__file__).resolve().parents[2] / "core" / "inbound_ingest.py"
    ).read_text(encoding="utf-8")
    assert "class SenderNotAllowed(DatastreamNotIngestable):" in ingest
    assert "read_delivery_sender(" in ingest
