"""Heuristic detection of the remote shell reached by ``fitt ssh test``.

Given the ``uname -a && pwd`` output from a successful SSH probe, classify
what shell environment served the command. The answer is useful when
debugging — "SSH worked but am I in Git Bash or WSL?" is a surprisingly
common question on Windows hosts.

The classifier is pure and stateless; it only looks at the strings. We
don't probe the remote for more info (no second RTT) — this is a best-
effort fast path that runs off data we already have.

Return categories:

* ``git-bash``    — MSYS / MinGW environment typical of Git Bash on Windows
* ``wsl``         — Linux kernel reached through WSL (drive under ``/mnt/c/``)
* ``linux``       — native Linux
* ``macos``       — native macOS (Darwin)
* ``bsd``         — FreeBSD / OpenBSD / NetBSD
* ``cmd``         — Windows cmd.exe (uname probably failed; pwd looks DOSish)
* ``unknown``     — couldn't tell; show the raw output
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShellDetection:
    """Result of classifying ``uname -a && pwd`` output."""

    kind: str
    """One of ``git-bash``, ``wsl``, ``linux``, ``macos``, ``bsd``,
    ``cmd``, ``unknown``."""

    label: str
    """Human-readable name — what to show the user."""

    uname_line: str
    """The first non-empty line of stdout; typically uname's output."""

    pwd_line: str
    """The second non-empty line; typically the remote home path."""


_KNOWN = {
    "git-bash": "Git Bash (MSYS)",
    "wsl": "WSL (Linux)",
    "linux": "Linux (native)",
    "macos": "macOS",
    "bsd": "BSD",
    "cmd": "Windows cmd.exe",
    "unknown": "unknown",
}


def detect_shell(stdout: str) -> ShellDetection:
    """Classify the remote shell from ``uname -a && pwd`` output.

    Lines are extracted defensively — a probe that produced a warning
    line first (e.g. a motd or a BatchMode=yes hint from sshd) still
    classifies correctly as long as uname + pwd are in there somewhere.
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    uname_line = ""
    pwd_line = ""
    # Find the uname-looking line (starts with a known kernel token).
    for ln in lines:
        low = ln.lower()
        if uname_line == "" and any(
            tok in low
            for tok in ("msys", "mingw", "linux", "darwin", "freebsd", "openbsd", "netbsd")
        ):
            uname_line = ln
            continue
        if pwd_line == "" and (ln.startswith("/") or (len(ln) > 1 and ln[1] == ":")):
            pwd_line = ln

    if not uname_line and lines:
        # Best effort: assume uname is first, pwd is second.
        uname_line = lines[0]
        if len(lines) >= 2 and not pwd_line:
            pwd_line = lines[1]

    kind = _classify(uname_line, pwd_line)
    return ShellDetection(
        kind=kind,
        label=_KNOWN[kind],
        uname_line=uname_line,
        pwd_line=pwd_line,
    )


def _classify(uname: str, pwd: str) -> str:
    """Map uname + pwd to one of the known kinds.

    Order matters: MSYS vs Linux on a Windows host both say ``Linux``
    in some edge-case WSL configs, so we check for MSYS markers first.
    """
    low = uname.lower()
    if "msys" in low or "mingw" in low:
        return "git-bash"
    if "linux" in low:
        # Distinguish WSL from native Linux by the shape of pwd.
        # WSL sees Windows drives at /mnt/<letter>/; native Linux never
        # does. If pwd starts /mnt/<letter>/ it's WSL reaching a Windows
        # drive. If pwd is under /home or /root it's native Linux (or
        # WSL with a proper Linux home — acceptable false-positive).
        if pwd.startswith("/mnt/") and len(pwd) > 6 and pwd[5].isalpha() and pwd[6] == "/":
            return "wsl"
        return "linux"
    if "darwin" in low:
        return "macos"
    if "freebsd" in low or "openbsd" in low or "netbsd" in low:
        return "bsd"
    # cmd.exe doesn't have `uname`; it prints 'uname' is not recognized.
    # Heuristic: pwd looks like a Windows path `C:\...`.
    if len(pwd) >= 3 and pwd[1:3] == ":\\":
        return "cmd"
    return "unknown"
