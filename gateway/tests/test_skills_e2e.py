"""End-to-end integration test for the skills loader (Phase 4.10, Commit 2).

Pins the operator-drops-markdown-and-it-works contract in one
test: write a SKILL.md to a tmp dir, point ``memory.skills_dir``
at it, send one chat request, assert the system prompt sent
upstream contains the ``[Skills available]`` block including
the skill's description and the absolute path to the SKILL.md.

Tagged with the conventions doc's phase + property markers
(Phase 4.10, Requirement 7) so the test's role is obvious.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import MemoryConfig

from ._fixtures import PERSONAL_TOKEN, build_test_config


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


class _FakeResponse:
    """Mimic the parts of ``litellm.ModelResponse`` chat.py touches."""

    def __init__(self) -> None:
        self.usage = type("Usage", (), {"prompt_tokens": 5, "completion_tokens": 2})()

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }


def _write_say_hello_skill(skills_dir: Path) -> Path:
    """Drop a minimal but valid 'say hello in French' skill.

    Returns the absolute path to the skill's SKILL.md so the
    test can assert it appears verbatim in the recipe-load hint.
    """
    sub = skills_dir / "say-hello-french"
    sub.mkdir(parents=True, exist_ok=True)
    skill_md = sub / "SKILL.md"
    skill_md.write_text(
        dedent(
            """\
            ---
            name: say-hello-french
            description: Say hello to someone in French.
            prerequisites: []
            ---

            # Say Hello in French

            When the user asks for a greeting in French, reply
            with `Bonjour, <name>!`. If they did not give you a
            name, ask first.
            """
        ),
        encoding="utf-8",
    )
    return skill_md.resolve()


def test_skill_appears_in_system_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 4.10, Requirement 7: SKILL.md drop → next request's
    system prompt sent upstream contains [Skills available] +
    the skill description + the absolute SKILL.md path."""

    # Operator drops a SKILL.md into the configured skills_dir.
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    skill_md_path = _write_say_hello_skill(skills_root)

    # Build a config that points memory.skills_dir at our drop.
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg = cfg.model_copy(
        update={
            "memory": cfg.memory.model_copy(
                update={
                    "skills_dir": skills_root,
                    "skills_enabled": True,
                }
            )
        }
    )

    app = create_app(cfg)
    client = TestClient(app)

    # Capture the upstream call so we can inspect the
    # system-prompt FITT actually sent.
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "say hello in French to Frédéric"}],
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text

    # Pull the system message out of what we actually sent
    # upstream. There may also be capability + identity content
    # merged into the same message.
    messages = captured["messages"]
    system_msgs = [m for m in messages if m.get("role") == "system"]
    assert system_msgs, f"no system message in upstream call: {messages!r}"
    system_text = system_msgs[0]["content"]

    # 1. The header is present (Requirement 3.2).
    assert "[Skills available]" in system_text, system_text

    # 2. The skill's line is present, prefixed correctly and
    #    carrying the description (Requirement 3.3 + the
    #    body of Requirement 7.2).
    assert "- say-hello-french: Say hello to someone in French." in system_text

    # 3. The recipe-load hint carries the literal absolute
    #    path of our SKILL.md (Requirement 7.3).
    expected_hint = f"(read recipe with read_file {skill_md_path})"
    assert expected_hint in system_text, (
        f"expected {expected_hint!r} in system message, got:\n{system_text}"
    )


def test_skills_disabled_omits_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Property 6: ``skills_enabled: false`` → system prompt
    contains no [Skills available] substring, regardless of
    what's on disk in skills_dir."""

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_say_hello_skill(skills_root)  # would-be loaded

    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg = cfg.model_copy(
        update={
            "memory": cfg.memory.model_copy(
                update={
                    "skills_dir": skills_root,
                    "skills_enabled": False,  # the toggle
                }
            )
        }
    )

    app = create_app(cfg)
    client = TestClient(app)

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(),
    )
    assert r.status_code == 200

    messages = captured["messages"]
    # Concatenate every system-role content; assert none of them
    # mentions the block. Capability + identity messages are
    # allowed; only [Skills available] must be absent.
    all_system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    assert "[Skills available]" not in all_system
    assert "say-hello-french" not in all_system


def test_default_skills_dir_resolves_under_fitt_home(tmp_path: Path) -> None:
    """The default ``MemoryConfig.skills_dir`` points at
    ``$FITT_HOME/skills`` so the loader works out of the box
    on a fresh install with no config override.

    This pins the contract that ``MemoryConfig`` defaults
    follow ``fitt_home()`` (which the autouse ``isolate_fitt_home``
    fixture has already redirected to a temp dir)."""
    cfg = MemoryConfig()
    assert cfg.skills_dir.name == "skills"
    assert cfg.skills_enabled is True
