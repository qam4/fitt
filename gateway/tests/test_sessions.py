"""Tests for the session registry and resolver (Phase 2.5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.errors import UnknownSession
from gateway.sessions import (
    DuplicateSessionId,
    InvalidSessionId,
    ProtectedSession,
    Session,
    SessionRegistry,
)

from ._fixtures import PERSONAL_TOKEN, build_test_config


def _registry(tmp_path: Path) -> SessionRegistry:
    d = tmp_path / "sessions"
    reg = SessionRegistry(d)
    reg.ensure_main()
    return reg


# ---------- ensure_main -------------------------------------------


def test_ensure_main_creates_index_if_missing(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert (tmp_path / "sessions" / "sessions.json").exists()
    assert {s.id for s in reg.all()} == {"main"}


def test_ensure_main_preserves_existing_sessions(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("retroai", "Retro-AI")
    # Simulate restart
    reg2 = SessionRegistry(tmp_path / "sessions")
    reg2.ensure_main()
    ids = {s.id for s in reg2.all()}
    assert ids == {"main", "retroai"}


# ---------- create ------------------------------------------------


def test_create_adds_session(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("retroai", "Retro-AI")
    got = reg.get("retroai")
    assert got is not None
    assert got.name == "Retro-AI"
    assert got.archived is False


def test_create_creates_history_dir(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("foo")
    assert (tmp_path / "sessions" / "foo" / "history").is_dir()


def test_create_rejects_bad_id(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for bad in ["Foo", "-leading", "has spaces", "a/b", "", "UPPER"]:
        with pytest.raises(InvalidSessionId):
            reg.create(bad)


def test_create_rejects_duplicate(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("foo")
    with pytest.raises(DuplicateSessionId):
        reg.create("foo")


def test_create_defaults_name_to_id(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("bar")
    assert reg.get("bar").name == "bar"


# ---------- rename ------------------------------------------------


def test_rename_updates_name_only(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("retroai", "Old")
    before = reg.get("retroai")
    reg.rename("retroai", "Retro-AI v2")
    after = reg.get("retroai")
    assert after.name == "Retro-AI v2"
    assert after.id == before.id
    assert after.created_at == before.created_at


def test_rename_main_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(ProtectedSession):
        reg.rename("main", "not allowed")


# ---------- archive / unarchive -----------------------------------


def test_archive_hides_from_valid_ids(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("foo")
    assert "foo" in reg.valid_ids()
    reg.archive("foo")
    assert "foo" not in reg.valid_ids()
    # Still in the on-disk list for audit / unarchive
    assert "foo" in {s.id for s in reg.all(include_archived=True)}


def test_unarchive_restores(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("foo")
    reg.archive("foo")
    reg.unarchive("foo")
    assert "foo" in reg.valid_ids()


def test_archive_main_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(ProtectedSession):
        reg.archive("main")


# ---------- corrupted index ---------------------------------------


def test_corrupted_index_falls_back_to_main(tmp_path: Path) -> None:
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "sessions.json").write_text("not valid json", encoding="utf-8")
    reg = SessionRegistry(d)
    # valid_ids should still contain main (in-memory fallback).
    assert "main" in reg.valid_ids()


# ---------- atomic write -----------------------------------------


def test_no_half_written_json(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for i in range(5):
        reg.create(f"s{i}")
    # After every operation the index parses cleanly.
    raw = json.loads((tmp_path / "sessions" / "sessions.json").read_text(encoding="utf-8"))
    ids = {s["id"] for s in raw["sessions"]}
    assert ids == {"main", "s0", "s1", "s2", "s3", "s4"}


# ---------- HTTP resolver ----------------------------------------


def _build_client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


def test_chat_without_header_defaults_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    async def fake_acompletion(**_: Any):
        class FakeResp:
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

            def model_dump(self, **__: Any):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        return FakeResp()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)
    client = _build_client(tmp_path)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.headers["X-FITT-Session"] == "main"


def test_chat_unknown_session_returns_400(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Session": "bogus",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "unknown_session"
    assert "main" in body["error"]["available"]


def test_chat_archived_session_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pre-create a session and archive it via a registry before
    # building the app. The app's registry reads the same file.
    cfg = build_test_config(tmp_path, memory_enabled=True)
    reg = SessionRegistry(cfg.memory.sessions_dir)
    reg.ensure_main()
    reg.create("retroai")
    reg.archive("retroai")

    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Session": "retroai",
        },
    )
    assert r.status_code == 400


def test_new_session_visible_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    async def fake_acompletion(**_: Any):
        class FakeResp:
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

            def model_dump(self, **__: Any):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        return FakeResp()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    cfg = build_test_config(tmp_path, memory_enabled=True)
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)

    # Before: 'foo' doesn't exist, request must be rejected.
    r1 = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Session": "foo",
        },
    )
    assert r1.status_code == 400

    # Create 'foo' via a separate registry pointed at the same dir
    # (simulating 'fitt session new foo' from another shell).
    SessionRegistry(cfg.memory.sessions_dir).create("foo")

    # After: same running app accepts the new session without
    # restart.
    r2 = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Session": "foo",
        },
    )
    assert r2.status_code == 200
    assert r2.headers["X-FITT-Session"] == "foo"


# ---------- history isolation ------------------------------------


def test_history_isolation_across_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Any

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any):
        captured.append(kwargs)

        class FakeResp:
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

            def model_dump(self, **__: Any):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        return FakeResp()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    cfg = build_test_config(tmp_path, memory_enabled=True)
    SessionRegistry(cfg.memory.sessions_dir).ensure_main()
    SessionRegistry(cfg.memory.sessions_dir).create("retroai")

    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {PERSONAL_TOKEN}"}

    # Seed session 'main' with a distinctive fact.
    client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "remember: pineapples in main"}],
        },
        headers={**auth, "X-FITT-Session": "main"},
    )
    # Seed session 'retroai' with a different fact.
    client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "remember: pears in retroai"}],
        },
        headers={**auth, "X-FITT-Session": "retroai"},
    )

    # Now chat again in retroai - its request payload must NOT
    # contain the pineapples fact from main.
    captured.clear()
    client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "what fruit?"}],
        },
        headers={**auth, "X-FITT-Session": "retroai"},
    )
    assert captured, "fake_acompletion was not invoked"
    serialised = json.dumps(captured[0].get("messages"))
    assert "pears in retroai" in serialised
    assert "pineapples" not in serialised


# ---------- session dataclass round-trip --------------------------


def test_session_roundtrip() -> None:
    s = Session(
        id="retroai",
        name="Retro-AI",
        created_at=datetime(2026, 5, 3, 18, 14, 22, tzinfo=UTC),
        archived=False,
    )
    raw = s.to_dict()
    assert raw["created_at"] == "2026-05-03T18:14:22Z"
    restored = Session.from_dict(raw)
    assert restored == s


def test_unknown_session_includes_active() -> None:
    e = UnknownSession("bogus", ["main", "retroai"])
    assert "bogus" in str(e)
    assert "main" in str(e)
    assert "retroai" in str(e)
