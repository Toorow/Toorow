"""Tests pour server/core/google_token_store.py (Story 18.1, AD-21).

Trois familles de tests :

  (a) UNIT (sans DB) -- le coeur crypto :
      - round-trip encrypt -> decrypt rend exactement le payload d'origine ;
      - le blob au repos != plaintext (le token n'apparait pas dans le blob) ;
      - une cle differente -> echec de dechiffrement PROPRE (GoogleTokenStoreError,
        message redige, jamais le ciphertext ni le plaintext) ;
      - aucun secret dans repr()/str() de GoogleToken ni dans les exceptions ;
      - aucun secret dans les logs (caplog captures, assert token absent).

  (b) LIVE POSTGRES (AI-37) -- contraintes reelles :
      - store/load/clear contre la vraie base (CHECK auth_path, colonne BYTEA) ;
      - ligne d'audit ecrite au clear (AD-14) ;
      - une connexion Nango existante n'est PAS affectee (auth_path defaut 'nango',
        blob NULL) -- non-regression AD-3.

  (c) GREP de non-regression : AUCUN fichier de ce commit ne contient un token de
      test realiste en clair ; seuls des placeholders manifestement factices
      ('tok_test_not_a_secret') sont utilises.

Regles respectees :
  AD-21 / NFR3 : zero token en clair (base, logs, erreurs, fixtures, repr).
  AI-37       : les chemins de contraintes (CHECK, BYTEA) sont testes sur la vraie DB.
  AI-03       : stdout ASCII-only (aucun print non-ASCII).
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Placeholders manifestement factices -- JAMAIS de vrai token dans ce fichier.
# (Le test grep (c) plus bas prouve l'absence de token realiste en clair.)
_FAKE_ACCESS = "tok_test_not_a_secret_access"
_FAKE_REFRESH = "tok_test_not_a_secret_refresh"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_key_env(tmp_path, monkeypatch):
    """Point tenant_keys at a throwaway local key dir (never touches infra/keys/)."""
    monkeypatch.setenv("TENANT_KEY_BACKEND", "local")
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path / "keys"))
    yield


# ---------------------------------------------------------------------------
# (a) UNIT -- crypto round-trip et non-fuite
# ---------------------------------------------------------------------------


def test_round_trip_returns_exact_payload(local_key_env):
    """encrypt -> decrypt rend exactement le token d'origine (round-trip effectif)."""
    from core.google_token_store import decrypt_token_payload, encrypt_token_payload

    payload = {
        "access_token": _FAKE_ACCESS,
        "refresh_token": _FAKE_REFRESH,
        "metadata": {"token_type": "Bearer"},
    }
    blob = encrypt_token_payload(payload, "proj_rt")
    out = decrypt_token_payload(blob, "proj_rt")

    assert out["access_token"] == _FAKE_ACCESS
    assert out["refresh_token"] == _FAKE_REFRESH
    assert out["metadata"] == {"token_type": "Bearer"}


def test_blob_is_not_plaintext(local_key_env):
    """Le blob au repos ne contient NI l'access NI le refresh en clair."""
    from core.google_token_store import encrypt_token_payload

    blob = encrypt_token_payload(
        {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH}, "proj_op"
    )
    assert isinstance(blob, bytes)
    assert _FAKE_ACCESS.encode() not in blob, "access token en clair dans le blob !"
    assert _FAKE_REFRESH.encode() not in blob, "refresh token en clair dans le blob !"
    # Un blob Fernet est du base64url : il ne contient pas d'octets de controle.
    assert blob != _FAKE_ACCESS.encode()


def test_wrong_key_fails_cleanly(local_key_env):
    """Un blob chiffre pour un projet ne se dechiffre pas avec la cle d'un autre.

    L'echec est PROPRE : GoogleTokenStoreError, message redige, jamais le ciphertext.
    """
    from core.google_token_store import (
        GoogleTokenStoreError,
        decrypt_token_payload,
        encrypt_token_payload,
    )

    blob = encrypt_token_payload(
        {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH}, "proj_a"
    )
    with pytest.raises(GoogleTokenStoreError) as exc_info:
        decrypt_token_payload(blob, "proj_b_different_key")

    msg = str(exc_info.value)
    assert "redacted" in msg.lower()
    # L'exception ne doit contenir ni le ciphertext ni un token.
    assert _FAKE_ACCESS not in msg
    assert _FAKE_REFRESH not in msg
    assert blob.decode("ascii", "ignore") not in msg


def test_tampered_blob_fails_cleanly(local_key_env):
    """Un blob altere echoue proprement (InvalidToken -> erreur redigee)."""
    from core.google_token_store import (
        GoogleTokenStoreError,
        decrypt_token_payload,
        encrypt_token_payload,
    )

    blob = bytearray(
        encrypt_token_payload(
            {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH}, "proj_t"
        )
    )
    blob[-1] ^= 0x01  # flip un bit -> HMAC invalide
    with pytest.raises(GoogleTokenStoreError) as exc_info:
        decrypt_token_payload(bytes(blob), "proj_t")
    assert "redacted" in str(exc_info.value).lower()


def test_empty_blob_raises_without_secret(local_key_env):
    """Un blob vide/None leve une erreur redigee, sans secret."""
    from core.google_token_store import GoogleTokenStoreError, decrypt_token_payload

    with pytest.raises(GoogleTokenStoreError) as exc_info:
        decrypt_token_payload(b"", "proj_x")
    assert "redacted" in str(exc_info.value).lower()


def test_repr_and_str_hide_secrets():
    """repr()/str() de GoogleToken ne contiennent JAMAIS de token."""
    from core.google_token_store import GoogleToken

    tok = GoogleToken(
        access_token=_FAKE_ACCESS,
        refresh_token=_FAKE_REFRESH,
        granted_scopes=["analytics.readonly"],
    )
    for rendered in (repr(tok), str(tok), f"{tok}", f"{tok!r}"):
        assert _FAKE_ACCESS not in rendered, "access token fuit dans repr/str !"
        assert _FAKE_REFRESH not in rendered, "refresh token fuit dans repr/str !"
        # La representation reste utile : longueurs + scopes visibles.
        assert "redacted" in rendered.lower()
        assert "analytics.readonly" in rendered


def test_dataclass_default_repr_would_leak_but_is_overridden():
    """Garde-fou : meme via %r de logging, le token n'apparait pas."""
    from core.google_token_store import GoogleToken

    tok = GoogleToken(access_token=_FAKE_ACCESS, refresh_token=_FAKE_REFRESH)
    logged = "%r" % (tok,)
    assert _FAKE_ACCESS not in logged
    assert _FAKE_REFRESH not in logged


def _full_log_dump(caplog) -> str:
    """review-18-1 F-3: aggregate EVERY leak surface of every record, not just
    getMessage() -- raw msg, args, formatted exception text and exc_info repr."""
    parts: list[str] = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.append(repr(getattr(r, "msg", "")))
        parts.append(repr(getattr(r, "args", "")))
        if r.exc_text:
            parts.append(r.exc_text)
        if r.exc_info:
            parts.append(repr(r.exc_info))
    return "\n".join(parts)


def test_no_secret_in_logs_on_encrypt(local_key_env, caplog):
    """Aucun token n'apparait dans les logs pendant le chiffrement."""
    from core.google_token_store import encrypt_token_payload

    with caplog.at_level(logging.DEBUG, logger="core.google_token_store"):
        encrypt_token_payload(
            {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH}, "proj_log"
        )
    joined = _full_log_dump(caplog)
    assert _FAKE_ACCESS not in joined
    assert _FAKE_REFRESH not in joined


def test_decrypt_rejects_unknown_blob_version(local_key_env):
    """review-18-1 F-2: un blob valide mais d'une version de format inconnue est
    REJETÉ (Fernet prouve l'intégrité, pas l'anti-rollback entre blobs valides)."""
    import json as _json

    from core import google_token_store as gts
    from cryptography.fernet import Fernet

    key = gts.get_tenant_key_backend().get_or_create_key("proj_ver")
    import base64

    fernet = Fernet(base64.urlsafe_b64encode(key))
    stale = _json.dumps(
        {"v": 999, "access_token": "tok_test_not_a_secret", "refresh_token": "", "metadata": {}}
    ).encode("utf-8")
    blob = fernet.encrypt(stale)
    with pytest.raises(gts.GoogleTokenStoreError, match="version"):
        gts.decrypt_token_payload(blob, "proj_ver")


def test_check_expected_project_rejects_mismatch():
    """review-18-1 F-1 (défense en profondeur): expected_project_id ≠ projet réel
    de la connexion → GoogleTokenStoreError, sans contenu de token."""
    from core import google_token_store as gts

    with pytest.raises(gts.GoogleTokenStoreError) as exc_info:
        gts._check_expected_project("proj_real", "proj_other")
    assert "token redacted" in str(exc_info.value)
    # None = pas de vérification demandée (l'identité est le devoir de l'appelant 18.3).
    gts._check_expected_project("proj_real", None)
    gts._check_expected_project("proj_real", "proj_real")


def test_key_backend_failure_is_redacted(monkeypatch):
    """Si tenant_keys leve, l'erreur remontee ne contient pas de secret."""
    from core import google_token_store as gts

    class _Boom:
        def get_or_create_key(self, project_id):
            raise RuntimeError(f"boom with {_FAKE_ACCESS}")  # secret dans la cause

    monkeypatch.setattr(gts, "get_tenant_key_backend", lambda: _Boom())
    with pytest.raises(gts.GoogleTokenStoreError) as exc_info:
        gts.encrypt_token_payload(
            {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH}, "proj_boom"
        )
    # Le message reecrit ne doit pas propager le contenu de la RuntimeError.
    assert _FAKE_ACCESS not in str(exc_info.value)
    assert "redacted" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# (b) LIVE POSTGRES (AI-37) -- contraintes reelles + audit + non-regression
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="TEST_POSTGRES_DSN not set/reachable -- skip live PG"
)


def _pg_env(monkeypatch):
    """Wire core.db + tenant_keys at the live test DB + a throwaway key dir."""
    dsn = os.environ["TEST_POSTGRES_DSN"]
    monkeypatch.setenv("PLATFORM_DB_URL", dsn)
    monkeypatch.setenv("TENANT_KEY_BACKEND", "local")


def _seed_connection(dsn: str, project_id: str, conn_id: str, auth_path: str) -> None:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Projet parent (FK-free sur connection_ref.project_id historiquement,
            # mais on insere un projet pour rester coherent avec l'audit FK).
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
                VALUES (%s, %s, %s, 'active', 'test', 'org_test_fixture')
                ON CONFLICT (id) DO NOTHING
                """,
                (project_id, f"tok-{project_id}", f"slug-{uuid.uuid4().hex[:8]}"),
            )
            cur.execute(
                """
                INSERT INTO app.connection_ref
                    (id, provider, nango_connection_id, project_id, auth_path, owner_org_id,
                        owner_identity)
                VALUES (%s, %s, %s, %s, %s, 'org_test_fixture', 'tester@example.com')
                """,
                (conn_id, "google-analytics", f"nango_{conn_id}", project_id, auth_path),
            )
        conn.commit()


def _cleanup(dsn: str, project_id: str, conn_id: str) -> None:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # app.audit_log append-only (FR12) + FK vers connection_ref : une
            # connexion auditee n'est pas hard-deletable (prod = soft-delete).
            # Cleanup BEST-EFFORT : les entites de test auditees restent.
            for sql, arg in (
                ("DELETE FROM app.connection_ref WHERE id = %s", conn_id),
                ("DELETE FROM app.projects WHERE id = %s", project_id),
            ):
                try:
                    cur.execute("SAVEPOINT cleanup_step")
                    cur.execute(sql, (arg,))
                    cur.execute("RELEASE SAVEPOINT cleanup_step")
                except Exception:  # noqa: BLE001 -- best-effort, FK audit_log
                    cur.execute("ROLLBACK TO SAVEPOINT cleanup_step")
        conn.commit()


@pg_available
def test_live_store_load_round_trip(monkeypatch):
    """store_google_token -> load_google_token rend le token sur la vraie DB (BYTEA)."""
    _pg_env(monkeypatch)
    monkeypatch.setenv(
        "TENANT_KEY_DIR",
        os.path.join(os.environ.get("TMP", "/tmp"), f"k_{uuid.uuid4().hex[:8]}"),
    )
    from datetime import datetime, timezone

    from core.google_token_store import load_google_token, store_google_token

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    try:
        _seed_connection(dsn, pid, cid, "nango")
        expiry = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        store_google_token(
            cid,
            {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH},
            expiry,
            ["analytics.readonly", "webmasters.readonly"],
        )
        tok = load_google_token(cid)
        assert tok.access_token == _FAKE_ACCESS
        assert tok.refresh_token == _FAKE_REFRESH
        assert set(tok.granted_scopes) == {"analytics.readonly", "webmasters.readonly"}
        assert tok.token_expiry == expiry
    finally:
        _cleanup(dsn, pid, cid)


@pg_available
def test_live_auth_path_check_constraint(monkeypatch):
    """Le CHECK auth_path rejette une valeur hors ('nango','google_direct')."""
    _pg_env(monkeypatch)
    import psycopg  # noqa: PLC0415

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    try:
        _seed_connection(dsn, pid, cid, "nango")
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "UPDATE app.connection_ref SET auth_path = 'bogus' WHERE id = %s",
                        (cid,),
                    )
            conn.rollback()
    finally:
        _cleanup(dsn, pid, cid)


@pg_available
def test_live_store_sets_google_direct_and_blob_is_opaque(monkeypatch):
    """Apres store : auth_path='google_direct', et la lecture brute du BYTEA sans la
    cle ne rend qu'un blob opaque (isolation effective)."""
    _pg_env(monkeypatch)
    monkeypatch.setenv(
        "TENANT_KEY_DIR",
        os.path.join(os.environ.get("TMP", "/tmp"), f"k_{uuid.uuid4().hex[:8]}"),
    )
    import psycopg  # noqa: PLC0415
    from core.google_token_store import store_google_token

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    try:
        _seed_connection(dsn, pid, cid, "nango")
        store_google_token(
            cid,
            {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH},
            None,
            ["adwords"],
        )
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT auth_path, encrypted_token_blob FROM app.connection_ref WHERE id = %s",
                    (cid,),
                )
                auth_path, blob = cur.fetchone()
        assert auth_path == "google_direct"
        assert blob is not None
        raw = bytes(blob)
        # Isolation : le token en clair n'est PAS lisible dans le BYTEA brut.
        assert _FAKE_ACCESS.encode() not in raw
        assert _FAKE_REFRESH.encode() not in raw
    finally:
        _cleanup(dsn, pid, cid)


@pg_available
def test_live_clear_purges_and_writes_audit(monkeypatch):
    """clear_google_token purge le blob (NULL) et ecrit une ligne d'audit (AD-14)."""
    _pg_env(monkeypatch)
    monkeypatch.setenv(
        "TENANT_KEY_DIR",
        os.path.join(os.environ.get("TMP", "/tmp"), f"k_{uuid.uuid4().hex[:8]}"),
    )
    import psycopg  # noqa: PLC0415
    from core.google_token_store import clear_google_token, store_google_token

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    try:
        _seed_connection(dsn, pid, cid, "nango")
        store_google_token(
            cid, {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH}, None, ["adwords"]
        )
        assert clear_google_token(cid, performed_by="tester@example.com") is True

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT auth_path, encrypted_token_blob, token_expiry, granted_scopes "
                    "FROM app.connection_ref WHERE id = %s",
                    (cid,),
                )
                auth_path, blob, expiry, scopes = cur.fetchone()
                assert auth_path == "nango"  # remis au defaut sur
                assert blob is None
                assert expiry is None
                assert scopes is None
                # Ligne d'audit ecrite (AD-14 On-Behalf-Of).
                cur.execute(
                    "SELECT action, identity FROM app.audit_log WHERE connection_ref = %s",
                    (cid,),
                )
                rows = cur.fetchall()
        assert any(
            action == "connection.revoked" and identity == "tester@example.com"
            for action, identity in rows
        ), "ligne d'audit de purge manquante"
    finally:
        _cleanup(dsn, pid, cid)


@pg_available
def test_live_nango_connection_unaffected(monkeypatch):
    """Une connexion Nango existante garde auth_path='nango' et un blob NULL
    (non-regression AD-3 : aucune colonne token peuplee pour les lignes Nango)."""
    _pg_env(monkeypatch)
    import psycopg  # noqa: PLC0415

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    try:
        _seed_connection(dsn, pid, cid, "nango")
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT auth_path, encrypted_token_blob, token_expiry, granted_scopes "
                    "FROM app.connection_ref WHERE id = %s",
                    (cid,),
                )
                auth_path, blob, expiry, scopes = cur.fetchone()
        assert auth_path == "nango"
        assert blob is None
        assert expiry is None
        assert scopes is None
    finally:
        _cleanup(dsn, pid, cid)


@pg_available
def test_live_clear_missing_connection_returns_false(monkeypatch):
    """clear sur une connexion inexistante retourne False (idempotent, pas d'erreur)."""
    _pg_env(monkeypatch)
    from core.google_token_store import clear_google_token

    assert clear_google_token(f"conn_absent_{uuid.uuid4().hex[:8]}") is False


# ---------------------------------------------------------------------------
# (c) GREP de non-regression -- aucun token realiste en clair dans le commit
# ---------------------------------------------------------------------------


def test_no_realistic_token_plaintext_in_story_files():
    """Aucun fichier livre par 18.1 ne contient de token realiste en clair.

    On verifie que les fichiers de ce commit :
      - n'embarquent PAS de secret Google typique (ya29., 1//, GOCSPX-, un JWT) ;
      - n'utilisent que des placeholders manifestement factices (tok_test_...).

    C'est le garde-fou anti-fuite que la review 18.5 (adversariale) exigera.
    """
    import re

    repo_root = Path(__file__).resolve().parents[3]
    # review-18-1 F-4: la liste DOIT suivre la section « Fichiers » de la story
    # (tout fichier livré par le commit 18.1, pas seulement le trio code/test/SQL).
    story_files = [
        repo_root / "server" / "core" / "google_token_store.py",
        repo_root / "server" / "tests" / "core" / "test_google_token_store.py",
        repo_root / "infra" / "nango" / "migrations" / "029_google_direct_token_store.sql",
        repo_root / "server" / "pyproject.toml",
        repo_root / "_bmad-output" / "implementation-artifacts" / "18-1-encrypted-token-store.md",
    ]

    # Motifs de tokens Google/OAuth realistes -- interdits en clair.
    forbidden = [
        re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),  # Google access token
        re.compile(r"1//[A-Za-z0-9_\-]{20,}"),  # Google refresh token
        re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),  # Google client secret
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    ]

    for path in story_files:
        assert path.exists(), f"fichier de story manquant : {path}"
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            m = pat.search(text)
            assert m is None, (
                f"token realiste en clair detecte dans {path.name} "
                f"(motif {pat.pattern!r}) -- utiliser un placeholder factice"
            )
        # Tout token de test present doit etre un placeholder factice.
        if "tok_test" in text:
            assert "not_a_secret" in text, (
                f"placeholder de token ambigu dans {path.name} : prefixer 'tok_test_not_a_secret'"
            )
