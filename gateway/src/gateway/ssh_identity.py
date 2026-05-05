"""SSH identity management for the gateway.

The gateway acts as an SSH *client* against project satellites
(laptops, desktops, cloud dev boxes). It needs a key pair so the
``ExecutionBackend`` can run commands non-interactively. This
module owns the lifecycle of that key:

* Chooses the right location (``$FITT_HOME/ssh/id_ed25519`` by
  default; override with ``FITT_SSH_KEY_PATH``).
* Generates the key on first boot if missing. Idempotent: doing it
  twice doesn't rotate or stomp an existing key.
* Enforces POSIX ``0600`` on the private half and ``0644`` on the
  public half so OpenSSH clients accept it.
* Exposes ``read_public_key()`` so the CLI can print the pubkey
  without re-running ssh-keygen.

The private key file never leaves ``$FITT_HOME``. Users who want to
use a pre-existing SSH identity set ``FITT_SSH_KEY_PATH`` to point
at their own key and we skip generation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


def default_key_path() -> Path:
    """Return ``$FITT_SSH_KEY_PATH`` or ``$FITT_HOME/ssh/id_ed25519``."""
    env = os.environ.get("FITT_SSH_KEY_PATH")
    if env:
        return Path(env)
    from .config import fitt_home

    return fitt_home() / "ssh" / "id_ed25519"


def public_key_path(private_key_path: Path) -> Path:
    """Standard OpenSSH pairs: ``foo`` + ``foo.pub``."""
    return private_key_path.with_suffix(private_key_path.suffix + ".pub")


async def ensure_key(
    key_path: Path | None = None,
    *,
    comment: str = "fitt-gateway",
) -> Path:
    """Generate the SSH key pair if it doesn't exist. Return the
    absolute private-key path either way.

    Never rotates: if the private key file is already present, we
    trust it — fixing a mangled permission is the only thing we
    do on a subsequent call.

    Runs ssh-keygen as a subprocess rather than reimplementing key
    generation; keeps the on-disk format identical to what an
    operator would get from the shell.
    """
    path = key_path or default_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        _ensure_perms(path)
        return path

    # No key yet. Generate one.
    _log.info("ssh.identity.generating", extra={"path": str(path)})
    # -N "" = no passphrase. The key lives inside $FITT_HOME,
    # which is already treated as "secrets-adjacent" (owns
    # secrets.yaml, session history). Adding a passphrase would
    # require a prompt at startup, which breaks headless boot.
    proc = await asyncio.create_subprocess_exec(
        "ssh-keygen",
        "-t",
        "ed25519",
        "-N",
        "",
        "-f",
        str(path),
        "-C",
        comment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh-keygen failed ({proc.returncode}): {stderr.decode('utf-8', 'replace').strip()}"
        )
    _log.info(
        "ssh.identity.generated",
        extra={"path": str(path), "stdout": stdout.decode("utf-8", "replace").strip()},
    )
    _ensure_perms(path)
    return path


def _ensure_perms(private_key_path: Path) -> None:
    """Set 0600 on the private key and 0644 on the public half.

    No-op on Windows filesystems where POSIX bits don't apply.
    SSH clients inside the gateway container run on Linux, so the
    path that matters (``/fitt/ssh/`` inside the container) is
    always POSIX. The operator may generate the key on the host
    under a Windows filesystem, in which case this is just a hint.
    """
    if os.name == "nt":
        return
    try:
        private_key_path.chmod(0o600)
    except OSError as exc:
        _log.warning(
            "ssh.identity.chmod_private_failed",
            extra={"path": str(private_key_path), "error": str(exc)},
        )
    pub = public_key_path(private_key_path)
    if pub.exists():
        try:
            pub.chmod(0o644)
        except OSError as exc:
            _log.warning(
                "ssh.identity.chmod_public_failed",
                extra={"path": str(pub), "error": str(exc)},
            )


def read_public_key(key_path: Path | None = None) -> str:
    """Return the public key line, or raise FileNotFoundError."""
    path = key_path or default_key_path()
    pub = public_key_path(path)
    return pub.read_text(encoding="utf-8").strip()
