"""Tunnel-ensure tests — injected probe/launch/sleep, no real tunnel."""

from __future__ import annotations

import gateway.tunnel as tunnel_mod
from gateway.tunnel import TUNNEL_CMD_ENV, ensure_tunnel

_URL = "http://localhost:11435/api/tags"


def _reach_seq(*bools: bool):
    """A reachability probe returning the given booleans in order (last
    value repeats)."""
    seq = list(bools)
    calls = {"n": 0}

    def _fn(url: str) -> bool:
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    return _fn


def _recording_launch():
    launched: list[str] = []
    return launched, (lambda cmd: launched.append(cmd))


def test_already_up_no_launch() -> None:
    launched, launch = _recording_launch()
    st = ensure_tunnel(
        _URL, cmd="whatever", reach=_reach_seq(True), launch=launch, sleep=lambda s: None
    )
    assert st.reachable and st.action == "already-up"
    assert launched == []


def test_no_cmd_when_unreachable_and_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(TUNNEL_CMD_ENV, raising=False)
    st = ensure_tunnel(_URL, reach=_reach_seq(False), launch=lambda c: None, sleep=lambda s: None)
    assert not st.reachable and st.action == "no-cmd"
    assert TUNNEL_CMD_ENV in st.detail


def test_starts_and_comes_up() -> None:
    launched, launch = _recording_launch()
    # unreachable, then reachable after the launch + one poll.
    st = ensure_tunnel(
        _URL,
        cmd="start-my-tunnel",
        reach=_reach_seq(False, True),
        launch=launch,
        sleep=lambda s: None,
    )
    assert st.reachable and st.action == "started"
    assert launched == ["start-my-tunnel"]


def test_launch_but_never_comes_up() -> None:
    launched, launch = _recording_launch()
    st = ensure_tunnel(
        _URL,
        cmd="start-my-tunnel",
        wait_s=3.0,
        poll_s=1.0,
        reach=_reach_seq(False),  # always unreachable
        launch=launch,
        sleep=lambda s: None,
    )
    assert not st.reachable and st.action == "failed"
    assert launched == ["start-my-tunnel"]


def test_cmd_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv(TUNNEL_CMD_ENV, "env-tunnel-cmd")
    launched, launch = _recording_launch()
    ensure_tunnel(_URL, reach=_reach_seq(False, True), launch=launch, sleep=lambda s: None)
    assert launched == ["env-tunnel-cmd"]


def test_default_reachable_handles_down_endpoint() -> None:
    # The real probe returns False (not raise) for an unreachable URL.
    assert tunnel_mod._default_reachable("http://127.0.0.1:1/api/tags") is False
