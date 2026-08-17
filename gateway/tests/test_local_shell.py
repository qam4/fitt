"""Tests for the Phase 4.7 local-shell probe.

Exercise :class:`LocalShellProbe` across four scenarios:

* native ``bash`` works — Linux / macOS / Windows with bash on PATH.
* native ``bash`` fails, Git Bash at the absolute path works.
* only ``wsl -- bash -lc`` works — Windows with WSL, no Git Bash.
* nothing works — bare Windows hub.

The probe is ``asyncio.subprocess``-based, so each test
monkeypatches :func:`asyncio.create_subprocess_exec` to shape
the probe's verdicts without spawning real children.

Also pins the caching invariant (:meth:`detect` is idempotent)
and the :meth:`ShellInterpreter.wrap` contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gateway.tools.local_shell import (
    LocalShellProbe,
    ShellInterpreter,
)

# --------------------------------------------------------------- fake proc


class _FakeProc:
    """Minimal asyncio-subprocess stand-in.

    The probe only reads ``.returncode`` and ``.communicate()``;
    everything else is a stub. We drive the (returncode, stdout)
    tuple per call via the monkeypatch factory below.
    """

    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - not exercised here
        pass

    async def wait(self) -> int:  # pragma: no cover - not exercised here
        return self.returncode


Behaviour = tuple[str, Any]
"""(kind, value) pair:
- ("ok", bytes) → fake proc that exits 0 with stdout == value.
- ("exit", int) → fake proc that exits with that code and empty stdout.
- ("missing", Exception()) → create_subprocess_exec raises that exception.
- ("timeout", None) → communicate() never returns; probe times out.
"""


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    behaviours: dict[tuple[str, ...], Behaviour],
) -> list[tuple[str, ...]]:
    """Monkeypatch ``asyncio.create_subprocess_exec`` so the
    probe's candidate list is driven by ``behaviours``.

    Keyed on the ``argv_prefix`` tuple that the probe passes in
    (everything up to and including ``-lc``). The probe appends
    one final argument (``"echo probe"``); we strip that so
    callers only have to name the prefix.

    Returns the list of prefixes the probe actually tried, in
    call order — lets tests assert that the fallback ordering
    is respected.
    """
    called: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **_: Any) -> _FakeProc:
        # Last arg is always the probe command itself.
        prefix = tuple(argv[:-1])
        called.append(prefix)
        beh = behaviours.get(prefix)
        if beh is None:
            raise AssertionError(
                f"probe tried unexpected prefix {prefix!r}; test didn't prepare a behaviour for it"
            )
        kind, value = beh
        if kind == "missing":
            assert isinstance(value, Exception)
            raise value
        if kind == "ok":
            return _FakeProc(returncode=0, stdout=value)
        if kind == "exit":
            return _FakeProc(returncode=int(value), stdout=b"")
        if kind == "timeout":
            # A fake proc whose communicate never resolves within
            # the probe's wait_for budget.
            class _Hanging(_FakeProc):
                async def communicate(self) -> tuple[bytes, bytes]:
                    await asyncio.sleep(60)
                    return b"", b""  # pragma: no cover

            return _Hanging(returncode=-1, stdout=b"")
        raise AssertionError(f"unknown behaviour kind: {kind!r}")

    monkeypatch.setattr(
        "gateway.tools.local_shell.asyncio.create_subprocess_exec",
        fake_exec,
    )
    return called


# --------------------------------------------------------------- detect


async def test_detect_bash_wins_when_it_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native ``bash -lc`` succeeds → first-choice label."""
    _patch_subprocess(
        monkeypatch,
        {("bash", "-lc"): ("ok", b"probe\n")},
    )
    result = await LocalShellProbe().detect()
    assert result.label == "bash"
    assert result.argv_prefix == ("bash", "-lc")
    assert result.available is True


async def test_detect_falls_back_to_git_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bash missing → Git Bash next in line. The test names
    the fallback ordering the probe is committed to."""
    calls = _patch_subprocess(
        monkeypatch,
        {
            ("bash", "-lc"): ("missing", FileNotFoundError("no bash")),
            (r"C:\Program Files\Git\bin\bash.exe", "-lc"): ("ok", b"probe\n"),
        },
    )
    result = await LocalShellProbe().detect()
    assert result.label == "git-bash"
    assert result.available is True
    assert calls == [
        ("bash", "-lc"),
        (r"C:\Program Files\Git\bin\bash.exe", "-lc"),
    ]


async def test_detect_falls_back_to_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bash + Git Bash both fail → WSL.

    WSL's subprocess shape is ``wsl -- bash -lc <cmd>``, so the
    prefix is the four-element tuple. Tests the full chain
    exhausts before landing on WSL.
    """
    _patch_subprocess(
        monkeypatch,
        {
            ("bash", "-lc"): ("missing", FileNotFoundError("no bash")),
            (r"C:\Program Files\Git\bin\bash.exe", "-lc"): (
                "missing",
                FileNotFoundError("no git bash"),
            ),
            ("wsl", "--", "bash", "-lc"): ("ok", b"probe\n"),
        },
    )
    result = await LocalShellProbe().detect()
    assert result.label == "wsl"
    assert result.argv_prefix == ("wsl", "--", "bash", "-lc")
    assert result.available is True


async def test_detect_returns_none_when_nothing_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every candidate fails → none. ``available`` flips false
    so callers can short-circuit with a readable error."""
    _patch_subprocess(
        monkeypatch,
        {
            ("bash", "-lc"): ("missing", FileNotFoundError("no bash")),
            (r"C:\Program Files\Git\bin\bash.exe", "-lc"): (
                "missing",
                FileNotFoundError("no git bash"),
            ),
            ("wsl", "--", "bash", "-lc"): (
                "missing",
                FileNotFoundError("no wsl"),
            ),
        },
    )
    result = await LocalShellProbe().detect()
    assert result.label == "none"
    assert result.available is False


async def test_detect_rejects_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate that spawns but exits nonzero doesn't count
    as working. Catches shells wrapped in broken launchers."""
    _patch_subprocess(
        monkeypatch,
        {
            ("bash", "-lc"): ("exit", 1),
            (r"C:\Program Files\Git\bin\bash.exe", "-lc"): ("ok", b"probe\n"),
        },
    )
    result = await LocalShellProbe().detect()
    assert result.label == "git-bash"


async def test_detect_rejects_missing_probe_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate that exits 0 but swallows stdout doesn't
    count — catches WSL installs that print their not-registered
    banner and exit clean."""
    _patch_subprocess(
        monkeypatch,
        {
            ("bash", "-lc"): ("ok", b"unrelated banner\n"),
            (r"C:\Program Files\Git\bin\bash.exe", "-lc"): ("ok", b"probe\n"),
        },
    )
    result = await LocalShellProbe().detect()
    assert result.label == "git-bash"


async def test_detect_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call to detect() doesn't re-spawn subprocesses.
    Pins the idempotence invariant so the probe can run on
    every request without a latency tax."""
    calls = _patch_subprocess(
        monkeypatch,
        {("bash", "-lc"): ("ok", b"probe\n")},
    )
    probe = LocalShellProbe()
    first = await probe.detect()
    second = await probe.detect()
    assert first is second
    assert len(calls) == 1, (
        "detect() should cache the first result; a second call must not re-spawn"
    )


async def test_preset_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests inject a fake via ``preset=...`` — detect() must
    NOT spawn any subprocesses in that case."""
    # If detect() calls create_subprocess_exec we'll know: the
    # patch raises via the AssertionError path for any prefix.
    _patch_subprocess(monkeypatch, {})
    preset = ShellInterpreter(
        label="fake",
        argv_prefix=("fake-shell",),
        available=True,
    )
    result = await LocalShellProbe(preset=preset).detect()
    assert result is preset


# --------------------------------------------------------------- wrap


def test_wrap_prepends_argv_prefix() -> None:
    interp = ShellInterpreter(
        label="bash",
        argv_prefix=("bash", "-lc"),
        available=True,
    )
    assert interp.wrap("echo hi") == ["bash", "-lc", "echo hi"]


def test_wrap_raises_when_unavailable() -> None:
    interp = ShellInterpreter.none()
    with pytest.raises(RuntimeError, match="no POSIX shell"):
        interp.wrap("echo hi")


# ------------------------------------------- negative-result TTL
#
# Caching "no shell" forever meant one flaky probe disabled project_shell
# on every local project until the gateway restarted, with no retry.
# Observed on the Windows hub: Git Bash intermittently fails to fork with
# cygwin Win32 error 299 / error 5, so the probe correctly concluded "no
# shell" — and that verdict then outlived the transient condition by hours.


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


async def test_a_successful_probe_is_cached_forever() -> None:
    """An interpreter that worked doesn't stop existing, so don't pay to
    re-probe it."""
    from gateway.tools.local_shell import LocalShellProbe

    clock = _FakeClock()
    probe = LocalShellProbe(clock=clock)
    calls = 0

    async def _works(prefix: tuple[str, ...]) -> bool:
        nonlocal calls
        calls += 1
        return True

    probe._works = _works  # type: ignore[method-assign]

    first = await probe.detect()
    calls_after_first = calls
    clock.advance(10_000.0)
    second = await probe.detect()

    assert first.available and second.available
    assert calls == calls_after_first, "a working interpreter was re-probed"


async def test_a_failed_probe_is_retried_after_the_ttl() -> None:
    """The fix. A transient fork failure must not outlive itself."""
    from gateway.tools.local_shell import _NEGATIVE_TTL_S, LocalShellProbe

    clock = _FakeClock()
    probe = LocalShellProbe(clock=clock)
    # Scripted by PASS, not by candidate: detect() tries every candidate,
    # so a per-call iterator would let the second candidate succeed on the
    # first pass and the test would prove nothing.
    forked_ok = False

    async def _works(prefix: tuple[str, ...]) -> bool:
        return forked_ok

    probe._works = _works  # type: ignore[method-assign]

    first = await probe.detect()
    assert not first.available, "expected the simulated fork failure to fail every candidate"

    forked_ok = True  # the transient condition clears

    clock.advance(_NEGATIVE_TTL_S + 1.0)
    second = await probe.detect()

    assert second.available, "the probe never retried after the TTL expired"


async def test_a_failed_probe_is_not_retried_before_the_ttl() -> None:
    """Each retry costs up to _PROBE_TIMEOUT_S per candidate, so a
    genuinely shell-less hub must not re-probe on every tool call."""
    from gateway.tools.local_shell import LocalShellProbe

    clock = _FakeClock()
    probe = LocalShellProbe(clock=clock)
    calls = 0

    async def _works(prefix: tuple[str, ...]) -> bool:
        nonlocal calls
        calls += 1
        return False

    probe._works = _works  # type: ignore[method-assign]

    await probe.detect()
    calls_after_first = calls
    clock.advance(1.0)
    await probe.detect()

    assert calls == calls_after_first, "re-probed inside the TTL"


async def test_a_preset_interpreter_is_never_probed() -> None:
    """Tests inject a fake; that must stay free."""
    from gateway.tools.local_shell import LocalShellProbe, ShellInterpreter

    probe = LocalShellProbe(preset=ShellInterpreter.none())

    async def _boom(prefix: tuple[str, ...]) -> bool:  # pragma: no cover
        raise AssertionError("probed despite a preset")

    probe._works = _boom  # type: ignore[method-assign]

    assert not (await probe.detect()).available
