"""Tests for the remote-shell classifier used by ``fitt ssh test``.

Pure function tests — no subprocess, no network. The classifier's
only input is the stdout string from a successful
``uname -a && pwd`` invocation.
"""

from __future__ import annotations

import pytest

from gateway.ssh_probe import detect_shell


def test_git_bash_on_windows() -> None:
    """Git Bash returns MSYS_NT as the kernel name, and pwd uses
    POSIX paths with /c/ style drive mapping."""
    out = "MSYS_NT-10.0-26200 LAPTOP-FOO 3.6.6-1cdd4371.x86_64 2026-01-15 22:20 UTC x86_64 Msys\n/c/Users/frede"
    d = detect_shell(out)
    assert d.kind == "git-bash"
    assert "Git Bash" in d.label
    assert "MSYS" in d.uname_line
    assert d.pwd_line == "/c/Users/frede"


def test_wsl_accessing_windows_drive() -> None:
    """WSL sees a Linux kernel but pwd under /mnt/<letter>/ tells us
    it's WSL reaching a Windows drive."""
    out = "Linux DESKTOP-BAR 5.15.0-microsoft-standard-WSL2 #1 SMP x86_64 GNU/Linux\n/mnt/c/Users/frede"
    d = detect_shell(out)
    assert d.kind == "wsl"
    assert "WSL" in d.label


def test_native_linux() -> None:
    """Linux kernel + home-ish pwd → native Linux."""
    out = "Linux hub 6.1.0-15-amd64 #1 SMP x86_64 GNU/Linux\n/home/fred"
    d = detect_shell(out)
    assert d.kind == "linux"
    assert "Linux" in d.label


def test_macos() -> None:
    """Darwin kernel → macOS."""
    out = "Darwin mac 23.4.0 Darwin Kernel Version 23.4.0 arm64\n/Users/frede"
    d = detect_shell(out)
    assert d.kind == "macos"
    assert d.label == "macOS"


def test_freebsd() -> None:
    out = "FreeBSD bsdhost 14.0-RELEASE FreeBSD 14.0-RELEASE amd64\n/home/fred"
    d = detect_shell(out)
    assert d.kind == "bsd"
    assert d.label == "BSD"


def test_windows_cmd_exe() -> None:
    """cmd.exe doesn't have uname; uname's error plus a Windows
    pwd (C:\\...) lands us on cmd."""
    # uname is not recognised; sshd still runs pwd, which on cmd
    # prints the drive path with backslashes.
    out = "'uname' is not recognized as an internal or external command\nC:\\Users\\frede"
    d = detect_shell(out)
    assert d.kind == "cmd"


def test_empty_stdout() -> None:
    """No output at all — classify as unknown without crashing."""
    d = detect_shell("")
    assert d.kind == "unknown"


def test_unknown_fallback() -> None:
    """Garbage in — graceful unknown out."""
    d = detect_shell("some random line\nanother one")
    assert d.kind == "unknown"


def test_motd_before_uname_does_not_fool_classifier() -> None:
    """A remote with a motd line before uname should still classify
    correctly — we search for the kernel token, not rely on ordering."""
    out = (
        "Welcome to the dev host — for access problems, see wiki.\n"
        "Linux hub 6.1.0-15-amd64 #1 SMP x86_64 GNU/Linux\n"
        "/home/fred"
    )
    d = detect_shell(out)
    assert d.kind == "linux"


@pytest.mark.parametrize(
    "uname_fragment,expected",
    [
        ("MSYS_NT", "git-bash"),
        ("MINGW64_NT", "git-bash"),
        ("Linux", "linux"),
        ("Darwin", "macos"),
        ("FreeBSD", "bsd"),
    ],
)
def test_kernel_name_drives_classification(uname_fragment: str, expected: str) -> None:
    """Each recognised kernel token maps to the right kind."""
    out = f"{uname_fragment} host 1.0 whatever\n/home/fred"
    assert detect_shell(out).kind == expected
