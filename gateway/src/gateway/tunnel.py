"""Optional tunnel-ensure for live eval runs.

Live harness/eval runs against an EC2-tunnelled alias need the tunnel up
(e.g. an AWS SSM port-forward local:11435 -> remote:11434). This helper
checks whether the DUT endpoint is reachable and, if not, runs an
**operator-configured** command to bring it up, then polls until it is.

Shareable by construction: the tunnel command is NOT in the repo — it
comes from the ``FITT_TUNNEL_CMD`` env var (or an explicit arg), so each
operator points FITT at their own script (e.g. an `ec2-ssm.sh`). The
command is launched detached (an SSM session blocks in the foreground),
and the launcher + reachability probe are injectable, so this is fully
unit-testable without a real tunnel.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

TUNNEL_CMD_ENV = "FITT_TUNNEL_CMD"

# url -> reachable?  |  cmd -> launch detached (no return)  |  seconds -> None
ReachFn = Callable[[str], bool]
LaunchFn = Callable[[str], None]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class TunnelStatus:
    """Outcome of :func:`ensure_tunnel`.

    ``action`` ∈ {``already-up``, ``started``, ``failed``, ``no-cmd``}."""

    reachable: bool
    action: str
    detail: str


def _default_reachable(url: str) -> bool:
    import httpx

    try:
        return httpx.get(url, timeout=5.0).status_code == 200
    except Exception:
        return False


def _default_launch(cmd: str) -> None:
    """Launch ``cmd`` detached from this process (it blocks / runs long)."""
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        subprocess.Popen(cmd, shell=True, creationflags=flags)
    else:
        subprocess.Popen(
            cmd,
            shell=True,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def ensure_tunnel(
    url: str,
    *,
    cmd: str | None = None,
    wait_s: float = 20.0,
    poll_s: float = 1.0,
    reach: ReachFn | None = None,
    launch: LaunchFn | None = None,
    sleep: SleepFn | None = None,
) -> TunnelStatus:
    """Ensure ``url`` is reachable, starting the tunnel if needed.

    * Already reachable -> ``already-up`` (no launch).
    * Unreachable + a command (arg or ``FITT_TUNNEL_CMD``) -> launch it
      detached, poll up to ``wait_s`` -> ``started`` or ``failed``.
    * Unreachable + no command -> ``no-cmd`` (caller decides: skip/warn).

    ``reach`` / ``launch`` / ``sleep`` are injectable for testing."""
    reach = reach or _default_reachable
    launch = launch or _default_launch
    sleep = sleep or time.sleep

    if reach(url):
        return TunnelStatus(True, "already-up", f"{url} already reachable")

    cmd = cmd or os.environ.get(TUNNEL_CMD_ENV)
    if not cmd:
        return TunnelStatus(
            False,
            "no-cmd",
            f"{url} unreachable and no tunnel command (set {TUNNEL_CMD_ENV})",
        )

    launch(cmd)
    waited = 0.0
    while waited < wait_s:
        sleep(poll_s)
        waited += poll_s
        if reach(url):
            return TunnelStatus(True, "started", f"tunnel up after ~{waited:.0f}s")
    return TunnelStatus(
        False, "failed", f"launched tunnel but {url} still unreachable after {wait_s:.0f}s"
    )
