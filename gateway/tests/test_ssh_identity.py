"""Tests for gateway/ssh_identity.py.

The module generates an SSH key pair via ``ssh-keygen`` on first
boot and leaves existing ones alone. We cover:

- key creation when none exists
- idempotence (second call preserves the existing key verbatim)
- permission enforcement on POSIX
- default_key_path's env-var override
- read_public_key round-trip

We skip most tests on Windows systems where ssh-keygen might not be
on PATH; the runtime container is Linux and CI is ubuntu-latest, so
the coverage that matters is still hit.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from gateway import ssh_identity

_SSH_KEYGEN_AVAILABLE = shutil.which("ssh-keygen") is not None


pytestmark = pytest.mark.skipif(
    not _SSH_KEYGEN_AVAILABLE,
    reason="ssh-keygen not on PATH; gateway runtime image installs it",
)


# --------------------------------------------------------------- defaults


def test_default_key_path_respects_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom_key"
    monkeypatch.setenv("FITT_SSH_KEY_PATH", str(override))
    assert ssh_identity.default_key_path() == override


def test_default_key_path_falls_back_to_fitt_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FITT_SSH_KEY_PATH", raising=False)
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    assert ssh_identity.default_key_path() == tmp_path / "ssh" / "id_ed25519"


def test_public_key_path_appends_pub() -> None:
    assert ssh_identity.public_key_path(Path("/x/key")) == Path("/x/key.pub")


# --------------------------------------------------------------- ensure_key


async def test_ensure_key_creates_pair_when_missing(tmp_path: Path) -> None:
    key = tmp_path / "ssh" / "id_ed25519"
    assert not key.exists()
    returned = await ssh_identity.ensure_key(key, comment="test-fixture")
    assert returned == key
    assert key.exists()
    assert (tmp_path / "ssh" / "id_ed25519.pub").exists()


async def test_ensure_key_preserves_existing(tmp_path: Path) -> None:
    """Second call must not overwrite the existing key.

    Protects against accidentally rotating a key that the satellite
    has already been authorised for — silent rotation would lock
    the gateway out of every satellite.
    """
    key = tmp_path / "ssh" / "id_ed25519"
    await ssh_identity.ensure_key(key, comment="first")
    first_priv = key.read_bytes()
    first_pub = ssh_identity.public_key_path(key).read_bytes()

    await ssh_identity.ensure_key(key, comment="second")
    assert key.read_bytes() == first_priv
    assert ssh_identity.public_key_path(key).read_bytes() == first_pub


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
async def test_ensure_key_sets_0600_on_private(tmp_path: Path) -> None:
    key = tmp_path / "ssh" / "id_ed25519"
    await ssh_identity.ensure_key(key)
    mode = key.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
async def test_ensure_key_sets_0644_on_public(tmp_path: Path) -> None:
    key = tmp_path / "ssh" / "id_ed25519"
    await ssh_identity.ensure_key(key)
    pub = ssh_identity.public_key_path(key)
    mode = pub.stat().st_mode & 0o777
    assert mode == 0o644, f"expected 0644, got {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
async def test_ensure_key_fixes_loose_private_perms(tmp_path: Path) -> None:
    """Existing key with 0644 perms gets tightened to 0600.

    Happens when a key was generated on the NAS host as a regular
    user and bind-mounted into the container; the host's umask may
    have left 0644 which OpenSSH will then refuse to load.
    """
    key = tmp_path / "ssh" / "id_ed25519"
    await ssh_identity.ensure_key(key)
    # Simulate host-side loose perms.
    key.chmod(0o644)
    assert key.stat().st_mode & 0o777 == 0o644

    await ssh_identity.ensure_key(key)
    mode = key.stat().st_mode & 0o777
    assert mode == 0o600


# --------------------------------------------------------------- read_public_key


async def test_read_public_key_returns_one_line(tmp_path: Path) -> None:
    key = tmp_path / "ssh" / "id_ed25519"
    await ssh_identity.ensure_key(key, comment="roundtrip")
    pub = ssh_identity.read_public_key(key)
    assert pub.startswith("ssh-ed25519 "), pub
    assert pub.endswith("roundtrip"), pub
    assert "\n" not in pub.rstrip("\n")


async def test_read_public_key_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ssh_identity.read_public_key(tmp_path / "no-such-key")


# --------------------------------------------------------------- perms helper


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_ensure_perms_is_noop_when_public_missing(tmp_path: Path) -> None:
    """Private key exists, .pub absent - _ensure_perms should not raise."""
    priv = tmp_path / "only_private"
    priv.write_bytes(b"dummy")
    # Start with permissive mode; the helper should tighten private.
    priv.chmod(0o644)
    ssh_identity._ensure_perms(priv)
    assert priv.stat().st_mode & 0o777 == 0o600
    # No .pub file was created.
    assert not priv.with_suffix(priv.suffix + ".pub").exists()


@pytest.mark.skipif(not hasattr(stat, "S_IMODE"), reason="stat unavailable")
def test_module_level_smoke() -> None:
    """Cheap smoke test that imports work and constants match shape."""
    assert ssh_identity.public_key_path(Path("/a/b")) == Path("/a/b.pub")
