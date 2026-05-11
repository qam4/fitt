"""Tests for :mod:`gateway.tool_artifacts` — the tool-output
hoisting layer.

Three concerns:

* Threshold behaviour: under threshold stays inline, over
  threshold gets hoisted + replaced with preview + footer.
* Disk shape: artifact lands at the documented session-scoped
  path, contains the full payload, round-trips UTF-8 including
  multi-byte characters.
* Graceful degradation: IO failure (sessions_dir unwritable,
  weird session key) falls back to pass-through rather than
  losing the data.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gateway.tool_artifacts import (
    DEFAULT_MAX_INLINE_BYTES,
    DEFAULT_PREVIEW_BYTES,
    ArtifactStore,
    default_artifact_dir,
)

# --------------------------------------------------------------- threshold


def test_small_payload_passes_through_unchanged(tmp_path: Path) -> None:
    """Payloads under the threshold must be returned byte-exact
    — no preview, no artifact on disk."""
    store = ArtifactStore(sessions_dir=tmp_path)
    payload = "ls output: a.txt b.txt c.txt\n"
    r = store.maybe_hoist(payload, session_key="main", tool_name="list_directory")
    assert r.content == payload
    assert r.artifact_path is None
    assert r.hoisted is False
    assert r.original_bytes == len(payload.encode("utf-8"))
    # No directories created when nothing to persist.
    assert not (tmp_path / "main").exists()


def test_large_payload_is_hoisted_to_disk(tmp_path: Path) -> None:
    """Over-threshold payloads land on disk with a short
    in-context replacement that names the artifact path."""
    store = ArtifactStore(
        sessions_dir=tmp_path,
        max_inline_bytes=100,
        preview_bytes=30,
    )
    payload = "A" * 10_000
    r = store.maybe_hoist(
        payload,
        session_key="coding",
        tool_name="read_file",
        now=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
    )
    assert r.hoisted is True
    assert r.artifact_path is not None
    assert r.original_bytes == 10_000
    # Preview + footer, not the full payload.
    assert len(r.content.encode("utf-8")) < 500
    assert r.content.startswith("A" * 30)
    assert "truncated" in r.content
    assert "10000 bytes" in r.content
    assert str(r.artifact_path) in r.content
    # Artifact lives under sessions/<key>/artifacts/<day>/
    expected_parent = tmp_path / "coding" / "artifacts" / "2026-05-11"
    assert r.artifact_path.parent == expected_parent.resolve()
    assert r.artifact_path.read_text(encoding="utf-8") == payload


def test_threshold_boundary_just_under_passes(tmp_path: Path) -> None:
    """A payload exactly at the threshold passes through. One
    byte over triggers hoisting. Covers off-by-one on the
    ``<=`` comparison."""
    store = ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=100, preview_bytes=30)
    at_threshold = "x" * 100
    over_threshold = "x" * 101
    r1 = store.maybe_hoist(at_threshold, session_key="s", tool_name="t")
    r2 = store.maybe_hoist(over_threshold, session_key="s", tool_name="t")
    assert r1.hoisted is False
    assert r2.hoisted is True


def test_preview_is_clamped_when_larger_than_threshold(tmp_path: Path) -> None:
    """A pathological config (preview > threshold) gets clamped
    so the preview never exceeds half the threshold. Otherwise
    hoisting could make the context LARGER than the original."""
    store = ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=100, preview_bytes=9999)
    assert store.preview_bytes <= 50
    payload = "y" * 10_000
    r = store.maybe_hoist(payload, session_key="s", tool_name="t")
    # After clamping + footer, the content must be smaller than
    # the original.
    assert len(r.content.encode("utf-8")) < len(payload.encode("utf-8"))


def test_invalid_thresholds_raise(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=0)
    with pytest.raises(ValueError):
        ArtifactStore(sessions_dir=tmp_path, preview_bytes=0)


# --------------------------------------------------------------- utf-8


def test_utf8_multibyte_preview_doesnt_split_characters(tmp_path: Path) -> None:
    """Preview must contain only whole characters — naive
    ``encode('utf-8')[:n]`` can split a 4-byte emoji in half
    and then crash on decode downstream."""
    store = ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=20, preview_bytes=6)
    # 8 4-byte emoji → 32 bytes, over threshold.
    payload = "🔥" * 8
    r = store.maybe_hoist(payload, session_key="s", tool_name="t")
    assert r.hoisted is True
    # content must decode as valid UTF-8 (no ``errors="replace"``
    # fallback). Python strings are already decoded, so the
    # right assertion is that every char in the preview is one
    # of the source characters — nothing was ``?`` or ``\ufffd``.
    preview_head = r.content.split("\n\n")[0]
    assert all(ch == "🔥" for ch in preview_head)
    assert "\ufffd" not in preview_head


# --------------------------------------------------------------- graceful


def test_io_failure_falls_back_to_passthrough(tmp_path: Path) -> None:
    """If we can't write the artifact (disk full, permission
    denied, whatever), return the payload unchanged with a
    warning rather than swallowing the content. Worse to lose
    data than to skip hoisting for one turn."""
    # Point sessions_dir at a path that can't be created — a
    # file where the parent expects a directory.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    store = ArtifactStore(sessions_dir=blocker, max_inline_bytes=10, preview_bytes=5)
    payload = "Q" * 1000
    r = store.maybe_hoist(payload, session_key="s", tool_name="t")
    assert r.hoisted is False
    assert r.content == payload
    assert r.artifact_path is None


def test_unsafe_session_or_tool_name_is_sanitized(tmp_path: Path) -> None:
    """Tool names and session keys are already validated
    upstream, but we defensively replace path separators and
    other shell-unsafe characters so a regression in a caller
    can't write outside the sessions_dir tree."""
    store = ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=10, preview_bytes=5)
    payload = "Z" * 200
    r = store.maybe_hoist(
        payload,
        session_key="../etc/passwd",
        tool_name="tool/../../bin/sh",
    )
    assert r.hoisted is True
    assert r.artifact_path is not None
    # Artifact stays anchored under sessions_dir.
    resolved_root = tmp_path.resolve()
    assert str(r.artifact_path).startswith(str(resolved_root))


# --------------------------------------------------------------- defaults


def test_default_thresholds_are_reasonable() -> None:
    """Sanity: defaults are what the docs claim, in case a
    future refactor changes the values silently."""
    assert DEFAULT_MAX_INLINE_BYTES == 8 * 1024
    assert DEFAULT_PREVIEW_BYTES == 2 * 1024


def test_default_artifact_dir_helper(tmp_path: Path) -> None:
    """Helper used by CLI / UI code to locate a session's
    artifact root returns the documented layout."""
    assert default_artifact_dir(tmp_path, "main") == tmp_path / "main" / "artifacts"


# --------------------------------------------------------------- concurrency


def test_concurrent_writes_produce_distinct_files(tmp_path: Path) -> None:
    """Two hoists with the same session/tool/day on the same
    store must land in two files — uuid4 collisions are
    astronomically unlikely, but we pin the behaviour so a
    future refactor that uses deterministic names breaks the
    test instead of silently overwriting data."""
    store = ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=10, preview_bytes=5)
    when = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    r1 = store.maybe_hoist("A" * 100, session_key="s", tool_name="read_file", now=when)
    r2 = store.maybe_hoist("B" * 100, session_key="s", tool_name="read_file", now=when)
    assert r1.artifact_path != r2.artifact_path
    assert r1.artifact_path is not None
    assert r2.artifact_path is not None
    assert r1.artifact_path.parent == r2.artifact_path.parent


# --------------------------------------------------------------- env helper


def test_artifact_path_is_absolute(tmp_path: Path) -> None:
    """The footer text is user-facing (operator greps for it in
    logs, model quotes it back at us); paths must be absolute
    so a copy-paste into any shell works regardless of CWD."""
    store = ArtifactStore(sessions_dir=tmp_path, max_inline_bytes=10, preview_bytes=5)
    r = store.maybe_hoist("X" * 500, session_key="s", tool_name="t")
    assert r.artifact_path is not None
    assert os.path.isabs(str(r.artifact_path))
