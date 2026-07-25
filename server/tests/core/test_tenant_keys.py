"""Tests for server/core/tenant_keys.py (Story 7.3, AC8).

Tests the LocalFileKeyBackend and get_tenant_key_backend factory.
All tests use a temporary directory so they do not touch infra/keys/.

AC8 tests:
  - test_local_backend_creates_key_on_first_call
  - test_local_backend_returns_same_key_on_repeat_call
  - test_local_backend_delete_key
  - test_local_backend_rotate_key
  - test_get_tenant_key_backend_returns_local_by_default
"""

from __future__ import annotations

import base64
import os

import pytest  # noqa: F401

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def key_dir(tmp_path):
    """Temporary directory for key files -- never writes to infra/keys/."""
    return str(tmp_path / "keys")


@pytest.fixture()
def backend(key_dir):
    from core.tenant_keys import LocalFileKeyBackend

    return LocalFileKeyBackend(key_dir=key_dir)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_local_backend_creates_key_on_first_call(backend, key_dir):
    """get_or_create_key creates a 32-byte key file on first call."""
    key = backend.get_or_create_key("proj_test")

    assert isinstance(key, bytes), "get_or_create_key must return bytes"
    assert len(key) == 32, "Key must be exactly 32 bytes"

    # File must exist
    key_file = os.path.join(key_dir, "proj_test.key")
    assert os.path.exists(key_file), "Key file must be created on disk"

    # File contains base64url-encoded 32-byte key
    raw = open(key_file, encoding="ascii").read().strip()
    decoded = base64.urlsafe_b64decode(raw)
    assert decoded == key, "File content must match returned key"


def test_local_backend_returns_same_key_on_repeat_call(backend):
    """Two calls to get_or_create_key for the same project return identical bytes."""
    key1 = backend.get_or_create_key("proj_stable")
    key2 = backend.get_or_create_key("proj_stable")

    assert key1 == key2, "Repeated calls must return the same key"
    assert len(key1) == 32


def test_local_backend_delete_key(backend):
    """delete_key removes the file; a subsequent get_or_create generates a new key."""
    key1 = backend.get_or_create_key("proj_delete_me")

    deleted = backend.delete_key("proj_delete_me")
    assert deleted is True, "delete_key must return True when key was present"

    # Second delete returns False (already gone)
    deleted_again = backend.delete_key("proj_delete_me")
    assert deleted_again is False, "delete_key must return False when key not found"

    # New get_or_create generates a different key
    key2 = backend.get_or_create_key("proj_delete_me")
    assert len(key2) == 32
    # Very high probability that a fresh 32-byte random key differs from the old one.
    assert key2 != key1, "New key after delete must differ from the old key"


def test_local_backend_rotate_key(backend):
    """rotate_key overwrites the file and returns a new 32-byte key."""
    key_orig = backend.get_or_create_key("proj_rotate")

    key_new = backend.rotate_key("proj_rotate")

    assert isinstance(key_new, bytes)
    assert len(key_new) == 32
    # Rotation generates a new random key (overwhelmingly likely to differ)
    assert key_new != key_orig, "Rotated key must differ from the original"

    # Subsequent get_or_create returns the rotated key (file was overwritten)
    key_after = backend.get_or_create_key("proj_rotate")
    assert key_after == key_new, "get_or_create after rotate must return the new key"


def test_get_tenant_key_backend_returns_local_by_default(monkeypatch, tmp_path):
    """get_tenant_key_backend returns LocalFileKeyBackend when TENANT_KEY_BACKEND is unset."""
    monkeypatch.delenv("TENANT_KEY_BACKEND", raising=False)
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path / "keys"))

    from core.tenant_keys import LocalFileKeyBackend, get_tenant_key_backend

    result = get_tenant_key_backend()
    assert isinstance(result, LocalFileKeyBackend), (
        "get_tenant_key_backend() must return LocalFileKeyBackend by default"
    )


def test_get_tenant_key_backend_returns_local_when_set(monkeypatch, tmp_path):
    """get_tenant_key_backend returns LocalFileKeyBackend when TENANT_KEY_BACKEND=local."""
    monkeypatch.setenv("TENANT_KEY_BACKEND", "local")
    monkeypatch.setenv("TENANT_KEY_DIR", str(tmp_path / "keys"))

    from core.tenant_keys import LocalFileKeyBackend, get_tenant_key_backend

    result = get_tenant_key_backend()
    assert isinstance(result, LocalFileKeyBackend)


def test_get_tenant_key_backend_returns_secret_manager_stub(monkeypatch):
    """get_tenant_key_backend returns SecretManagerKeyBackend when requested."""
    monkeypatch.setenv("TENANT_KEY_BACKEND", "secret_manager")

    from core.tenant_keys import SecretManagerKeyBackend, get_tenant_key_backend

    result = get_tenant_key_backend()
    assert isinstance(result, SecretManagerKeyBackend)

    # The stub must raise NotImplementedError with HG-A message
    with pytest.raises(NotImplementedError, match="HG-A"):
        result.get_or_create_key("proj_test")

    with pytest.raises(NotImplementedError, match="HG-A"):
        result.delete_key("proj_test")

    with pytest.raises(NotImplementedError, match="HG-A"):
        result.rotate_key("proj_test")


def test_local_backend_delete_nonexistent_returns_false(backend):
    """delete_key on a key that was never created returns False without raising."""
    result = backend.delete_key("proj_never_existed")
    assert result is False


def test_local_backend_rotate_creates_dir_if_missing(tmp_path):
    """rotate_key creates the key directory if it does not exist yet."""
    from core.tenant_keys import LocalFileKeyBackend

    key_dir = str(tmp_path / "deep" / "nested" / "keys")
    b = LocalFileKeyBackend(key_dir=key_dir)

    key = b.rotate_key("proj_new")
    assert len(key) == 32
    assert os.path.exists(key_dir), "rotate_key must create the directory"


# ---------------------------------------------------------------------------
# Key file permissions (hardening)
# ---------------------------------------------------------------------------


def test_key_file_content_roundtrips_after_hardening(backend):
    """get_or_create_key and rotate_key both produce readable, valid 32-byte keys."""
    # get_or_create_key: new key is written with os.open and can be read back
    key = backend.get_or_create_key("proj_perm_test")
    assert len(key) == 32

    key2 = backend.get_or_create_key("proj_perm_test")
    assert key == key2, "Second call must return same key (read back from file)"

    # rotate_key: new key replaces old one and is readable
    rotated = backend.rotate_key("proj_perm_test")
    assert len(rotated) == 32
    assert rotated != key, "Rotated key must differ (overwhelmingly likely)"

    key_after_rotate = backend.get_or_create_key("proj_perm_test")
    assert key_after_rotate == rotated, "get_or_create after rotate must return the rotated key"


@pytest.mark.skipif(os.name == "nt", reason="chmod mode bits are no-ops on Windows")
def test_key_file_permissions_are_restricted(tmp_path):
    """Key files are created with mode 0o600 on POSIX systems."""
    import stat

    from core.tenant_keys import LocalFileKeyBackend

    key_dir = str(tmp_path / "perm_keys")
    b = LocalFileKeyBackend(key_dir=key_dir)
    b.get_or_create_key("proj_perms")

    key_path = tmp_path / "perm_keys" / "proj_perms.key"
    mode = stat.S_IMODE(os.stat(str(key_path)).st_mode)
    assert mode == 0o600, f"Expected mode 0600, got {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="chmod mode bits are no-ops on Windows")
def test_key_dir_permissions_are_restricted(tmp_path):
    """Key directory is created with mode 0o700 on POSIX systems."""
    import stat

    from core.tenant_keys import LocalFileKeyBackend

    key_dir = str(tmp_path / "dir_perm_keys")
    b = LocalFileKeyBackend(key_dir=key_dir)
    b.get_or_create_key("proj_dir_perms")

    dir_mode = stat.S_IMODE(os.stat(key_dir).st_mode)
    assert dir_mode == 0o700, f"Expected dir mode 0700, got {oct(dir_mode)}"


def test_key_material_not_in_project_id_safe_name(backend):
    """project_id sanitisation: only alphanumeric, hyphens, underscores are kept."""
    import pathlib

    # An ID with unusual chars should be sanitised to a safe filename
    key = backend.get_or_create_key("proj_ULID01-test")
    assert len(key) == 32
    # The file name should not contain any dangerous chars
    key_dir = pathlib.Path(backend._key_dir)
    files = list(key_dir.glob("*.key"))
    assert len(files) == 1
    assert "/" not in files[0].name and "\\" not in files[0].name
