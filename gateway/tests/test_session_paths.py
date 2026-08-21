"""Tests for the shared per-session directory helper.

The defect these exist for: a cron firing's session key is
``cron:<id>:<ts>``, a colon is illegal in a Windows path component, and
three of the four per-session stores built their path by string
concatenation. Every write raised ``OSError``, was caught, logged at
warning level, and dropped — so on Windows ``fitt watch``, ``/lastturn``,
the dashboard turn detail and turn capture were all blind to scheduled
jobs. Found 2026-08-19 in an eval log (sixteen ``turns.append_failed``
per firing), never caught by a test, because the existing cron tests
assert on the event log and the audit log rather than on turn files.

So the tests below are deliberately written against the *stores*, not
just the helper: a correct helper that nobody calls would leave the bug
exactly where it was.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gateway.session_paths import safe_session_dirname, session_dir
from gateway.turns import TurnLog, new_event

CRON_KEY = "cron:abc123:1786000000"

# --------------------------------------------------------------- the helper


def test_a_cron_session_key_becomes_a_legal_directory_name() -> None:
    assert ":" not in safe_session_dirname(CRON_KEY)
    assert safe_session_dirname(CRON_KEY) == "cron_abc123_1786000000"


def test_an_ordinary_session_key_is_left_exactly_alone() -> None:
    """The whole reason this replaces only *reserved* characters. A
    stricter allowlist would rename directories that work today and
    orphan real history for no defect."""
    for key in ("main", "telegram-12345", "e2e-reminder-0", "memory_recall-0-b"):
        assert safe_session_dirname(key) == key


def test_characters_that_are_legal_everywhere_survive() -> None:
    """`@` and `+` are fine on every platform FITT runs on, so touching
    them would be a migration with no bug behind it."""
    assert safe_session_dirname("user@host+1") == "user@host+1"


def test_a_separator_cannot_smuggle_a_session_into_a_subdirectory() -> None:
    """A key carrying `/` would silently write one level down, and one
    carrying `..` would climb out of the sessions root entirely."""
    assert safe_session_dirname("a/b") == "a_b"
    out = safe_session_dirname("../../etc")
    assert "/" not in out and "\\" not in out


def test_an_empty_or_all_reserved_key_falls_back(tmp_path: Path) -> None:
    """Never return "", or the store would write into the sessions root
    and scatter one session's files across every other session's parent."""
    assert safe_session_dirname("") == "unknown"
    assert safe_session_dirname("///") == "___"
    assert session_dir(tmp_path, "") == tmp_path / "unknown"


def test_trailing_dots_and_spaces_are_dropped() -> None:
    """Windows strips them silently, so "foo." and "foo" would be the same
    directory on one platform and two on another."""
    assert safe_session_dirname("foo. ") == "foo"


# --------------------------------------------------------------- the stores
#
# These are the tests that would have caught the original defect.


def test_a_cron_firings_turn_events_actually_land_on_disk(tmp_path: Path) -> None:
    """The exact failure: TurnLog.append swallows OSError, so the only way
    to see this bug is to check the file exists afterwards."""
    log = TurnLog(tmp_path)

    log.append(new_event(kind="turn_started", session_key=CRON_KEY, turn_id="t1"))

    written = list(tmp_path.rglob("*.jsonl"))
    assert written, (
        "a cron firing's turn event was dropped — TurnLog swallowed the "
        f"write error. tree: {list(tmp_path.rglob('*'))}"
    )
    assert "t1" in written[0].read_text(encoding="utf-8")


def test_the_turn_log_can_read_back_what_it_wrote_for_a_cron_session(
    tmp_path: Path,
) -> None:
    """Write and read must agree on the sanitised path. If only one side
    went through the helper, the file would exist and be invisible — which
    is worse than the original bug, because nothing would warn."""
    log = TurnLog(tmp_path)
    log.append(new_event(kind="turn_started", session_key=CRON_KEY, turn_id="t9"))

    day = datetime.now(tz=UTC).date()
    assert log.file_path(CRON_KEY, day).exists()
    assert [e.turn_id for e in log.read(CRON_KEY)] == ["t9"]


def test_memory_history_path_is_legal_for_a_cron_session(tmp_path: Path) -> None:
    """cron.memory_append_failed came from the same root cause, one line
    after the turn-event warnings."""
    from gateway.memory import MemoryStore

    store = MemoryStore(
        identity_dir=tmp_path / "identity",
        sessions_dir=tmp_path / "sessions",
        max_history_chars=10_000,
        enabled=True,
    )
    path = store.history_path(CRON_KEY)

    assert ":" not in path.name and ":" not in path.parent.parent.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok", encoding="utf-8")  # would raise OSError before the fix


def test_turn_capture_dir_is_legal_for_a_cron_session(tmp_path: Path) -> None:
    from gateway.turn_capture import TurnCaptureStore

    store = TurnCaptureStore(tmp_path)
    d = store.turn_dir(CRON_KEY, datetime.now(tz=UTC))

    assert ":" not in str(d.relative_to(tmp_path))
    d.mkdir(parents=True, exist_ok=True)


def test_artifacts_keep_the_layout_they_already_had(tmp_path: Path) -> None:
    """tool_artifacts already sanitised, so adopting the shared helper must
    not relocate anything. For a cron key both rules agree."""
    from gateway.tool_artifacts import default_artifact_dir

    assert default_artifact_dir(tmp_path, CRON_KEY) == (
        tmp_path / "cron_abc123_1786000000" / "artifacts"
    )
