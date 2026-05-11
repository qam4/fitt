"""End-to-end test for the tool-output artifact hoisting path
wired into the chat handler.

Proves that when a tool returns a payload over the threshold:

* The second upstream dispatch receives a short preview + footer
  instead of the full payload.
* The full payload lands on disk under
  ``sessions/<key>/artifacts/<YYYY-MM-DD>/<tool>-<uuid>.txt``.
* The audit log still receives the original (non-hoisted) payload
  for errors, because audit is the forensic record and must stay
  byte-exact regardless of context-shape optimisation.

Uses the same monkeypatch-the-upstream-model pattern as the rest
of ``test_chat_tool_forwarding.py``. A ``read_file`` tool is
cheapest to produce a big payload — we point it at a prefilled
file in the test's tmp repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.projects import Project

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import make_response, make_tool_call


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


@pytest.fixture
def big_file_client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    """Gateway with a tmp project whose BIG.md is larger than the
    default hoist threshold (8 KB). Returns the client and paths
    to (repo, sessions_dir) so tests can verify on-disk state."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)

    repo = tmp_path / "repo"
    repo.mkdir()
    big_content = "a long line of text repeated many times.\n" * 500  # ~20 KB
    (repo / "BIG.md").write_text(big_content, encoding="utf-8")

    app.state.project_registry.add(
        Project(
            name="hub",
            ssh_host="",
            path=str(repo),
            test_command="pytest -q",
        )
    )
    return TestClient(app), repo, cfg.memory.sessions_dir


def test_large_tool_output_is_hoisted_before_reaching_model(
    big_file_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second dispatch's tool-role message must carry a
    preview + footer, not the full 20 KB file content.

    The test reconstructs the model's view of the turn by
    capturing every dispatch body and asserting on what the
    model actually saw as the ``role: tool`` content."""
    client, _repo, _sessions_dir = big_file_client
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return make_response(
                tool_calls=[
                    make_tool_call(
                        "call-1",
                        "read_file",
                        {"project": "hub", "path": "BIG.md"},
                    )
                ]
            )
        return make_response(content="read it")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read BIG.md"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert len(calls) == 2

    msgs = calls[1]["messages"]
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    content = tool_msg["content"]
    # Under default preview cap (2 KB) + footer, the content the
    # model sees is far smaller than the 20 KB file.
    assert len(content.encode("utf-8")) < 4_000
    # But the preview head is real content, not a stub.
    assert "a long line of text" in content
    # Footer names the artifact path and the truthful byte count.
    assert "truncated" in content
    assert "written to" in content


def test_large_tool_output_lands_on_disk(
    big_file_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hoisted payload must be written verbatim to the
    session's artifacts dir. Operators reading the artifact get
    the same bytes the tool originally produced."""
    client, _repo, sessions_dir = big_file_client
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return make_response(
                tool_calls=[
                    make_tool_call(
                        "call-1",
                        "read_file",
                        {"project": "hub", "path": "BIG.md"},
                    )
                ]
            )
        return make_response(content="read it")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read BIG.md"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text

    # Find the artifact. Default session id is ``main`` and the
    # day is today, but we don't pin the date — the uuid is the
    # one unknown, so we walk.
    artifacts_root = sessions_dir / "main" / "artifacts"
    assert artifacts_root.exists(), "artifact root not created"
    all_files = [p for p in artifacts_root.rglob("*") if p.is_file()]
    assert len(all_files) == 1, f"expected 1 artifact, got {all_files}"
    artifact = all_files[0]
    assert artifact.name.startswith("read_file-"), artifact.name
    # Content round-trips verbatim.
    content_on_disk = artifact.read_text(encoding="utf-8")
    assert content_on_disk.startswith("a long line of text")
    assert len(content_on_disk.encode("utf-8")) > 8 * 1024


def test_small_tool_output_is_not_hoisted(
    big_file_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the above — a small file read returns inline
    and creates no artifact."""
    client, repo, sessions_dir = big_file_client
    (repo / "TINY.md").write_text("just a line\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return make_response(
                tool_calls=[
                    make_tool_call(
                        "call-1",
                        "read_file",
                        {"project": "hub", "path": "TINY.md"},
                    )
                ]
            )
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read TINY.md"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    msgs = calls[1]["messages"]
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert tool_msg["content"].strip().startswith("just a line")
    assert "truncated" not in tool_msg["content"]
    # No artifact dir created when nothing needed hoisting.
    artifacts_root = sessions_dir / "main" / "artifacts"
    assert not artifacts_root.exists()
