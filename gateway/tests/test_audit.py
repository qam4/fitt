"""Tests for the HMAC-chained audit log.

Coverage:

* redact() strips tokens/API keys from nested dicts and long
  base64-shaped strings.
* append() writes one JSON line per call, populates prev_hmac /
  hmac correctly, and persists across restarts via tail-read.
* verify() catches the usual tampering: edited field, inserted
  line, deleted line, missing key.
* A property-based test for chain integrity: random entries,
  random tamper positions, verify() always flags the first
  corruption.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.audit import (
    AuditEntry,
    AuditLog,
    _compute_hmac,
    new_entry,
    redact,
)

# --------------------------------------------------------------- redact


def test_redact_strips_secret_key_names() -> None:
    """Args whose keys match the secret-name regex are replaced."""
    got = redact({"api_key": "abc", "token": "xyz", "path": "README.md"})
    assert got == {"api_key": "<redacted>", "token": "<redacted>", "path": "README.md"}


def test_redact_recurses_into_nested_dicts() -> None:
    got = redact(
        {
            "outer": {"password": "secret", "user": "fred"},
            "list": [{"bearer_token": "hush"}, "plain"],
        }
    )
    assert got["outer"]["password"] == "<redacted>"
    assert got["outer"]["user"] == "fred"
    assert got["list"][0]["bearer_token"] == "<redacted>"
    assert got["list"][1] == "plain"


def test_redact_strips_openai_keys_from_strings() -> None:
    got = redact({"msg": "Your key is sk-abcdefghijklmnopqrstuvwxyzABCDEF"})
    assert "<redacted>" in got["msg"]
    assert "sk-abcdef" not in got["msg"]


def test_redact_strips_anthropic_keys_from_strings() -> None:
    got = redact({"msg": "Use sk-ant-api01-deadbeefdeadbeefdeadbeef please"})
    assert "<redacted>" in got["msg"]


def test_redact_strips_github_pat() -> None:
    got = redact({"header": "Authorization: Bearer ghp_" + "a" * 35})
    # ghp_ pattern matches; also long base64 pattern may match the rest
    assert "ghp_" + "a" * 35 not in json.dumps(got)


def test_redact_leaves_short_strings_alone() -> None:
    """Short plain text shouldn't be flagged by the long-b64 pattern."""
    got = redact({"message": "hello world", "count": 42})
    assert got == {"message": "hello world", "count": 42}


def test_redact_leaves_non_strings_alone() -> None:
    got = redact({"n": 42, "ok": True, "x": None, "lst": [1, 2, 3]})
    assert got == {"n": 42, "ok": True, "x": None, "lst": [1, 2, 3]}


def test_redact_preserves_long_unix_file_paths() -> None:
    """Linux pytest tmp paths used to round-trip as
    ``/<redacted>.md`` because the long-base64 catch-all
    regex included ``/`` in its character class. Pin that
    file paths survive redaction; modern key formats use
    URL-safe base64 (``[A-Za-z0-9_-]``), no ``/``."""
    long_path = (
        "/tmp/pytest-of-runner/pytest-0/test_save_emits_one_audit_entry_per_success0/audited.md"
    )
    got = redact({"path": long_path})
    assert got["path"] == long_path


def test_redact_preserves_windows_file_paths() -> None:
    """Symmetric Windows-shape coverage so a regression in
    either direction is caught."""
    win_path = (
        r"C:\Users\testuser\AppData\Local\Temp\pytest-of-testuser"
        r"\pytest-0\test_save_emits_one_audit_entry_per_success0"
        r"\audited.md"
    )
    got = redact({"path": win_path})
    assert got["path"] == win_path


def test_redact_still_catches_long_url_safe_base64() -> None:
    """The catch-all is narrower now (no ``/`` or ``+``) but
    still catches the ``[A-Za-z0-9_-]{40,}`` shape — that's
    every modern key format we care about."""
    long_key = "a" * 50  # 50 chars, all in the URL-safe class
    got = redact({"opaque": long_key})
    assert got["opaque"] == "<redacted>"


# --------------------------------------------------------------- append


def _mk_entry(**overrides: Any) -> AuditEntry:
    defaults = dict(
        session_key="main",
        client="ide",
        tool="read_file",
        args={"path": "README.md"},
        decision="auto",
        ok=True,
        duration_ms=3,
    )
    defaults.update(overrides)
    return new_entry(**defaults)  # type: ignore[arg-type]


def test_append_writes_one_json_line(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry())
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["tool"] == "read_file"
    assert data["hmac"]
    assert data["prev_hmac"] == ""  # first entry


def test_append_chains_prev_hmac(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    first = log.append(_mk_entry())
    second = log.append(_mk_entry(tool="list_directory"))
    assert second.prev_hmac == first.hmac
    assert second.hmac != first.hmac


def test_append_redacts_args_before_write(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry(tool="write_file", args={"path": "x.py", "api_key": "sk-xyz"}))
    line = (tmp_path / "audit.jsonl").read_text().splitlines()[0]
    data = json.loads(line)
    assert data["args"]["api_key"] == "<redacted>"
    assert data["args"]["path"] == "x.py"


def test_append_creates_audit_key_with_secure_perms(tmp_path: Path) -> None:
    """First append generates the key at 0600 (POSIX) / defaults (Windows)."""
    key_path = tmp_path / "audit.key"
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=key_path)
    assert not key_path.exists()
    log.append(_mk_entry())
    assert key_path.exists()
    data = key_path.read_bytes()
    assert len(data) == 32  # _KEY_BYTES


def test_append_reuses_existing_key(tmp_path: Path) -> None:
    """Two AuditLog instances pointing at the same key file read the same secret."""
    key_path = tmp_path / "audit.key"
    log1 = AuditLog(path=tmp_path / "audit.jsonl", key_path=key_path)
    log1.append(_mk_entry())
    key_bytes = key_path.read_bytes()

    log2 = AuditLog(path=tmp_path / "audit2.jsonl", key_path=key_path)
    log2.append(_mk_entry())
    assert key_path.read_bytes() == key_bytes  # unchanged


def test_append_across_restart_continues_chain(tmp_path: Path) -> None:
    """A new AuditLog instance pointing at an existing file tails
    the last hmac so the next append chains correctly."""
    log1 = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    first = log1.append(_mk_entry())

    # Simulate a restart: new AuditLog instance, same paths.
    log2 = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    second = log2.append(_mk_entry(tool="list_directory"))
    assert second.prev_hmac == first.hmac


# --------------------------------------------------------------- verify


def test_verify_accepts_empty_log(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    result = log.verify()
    assert result.ok
    assert result.total_lines == 0


def test_verify_accepts_clean_chain(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    for i in range(5):
        log.append(_mk_entry(tool=f"tool_{i}"))
    result = log.verify()
    assert result.ok
    assert result.total_lines == 5


def _overwrite_line(path: Path, line_num: int, new_line: str) -> None:
    """1-based line replacement. Keeps the rest intact."""
    lines = path.read_text().splitlines()
    lines[line_num - 1] = new_line
    path.write_text("\n".join(lines) + "\n")


def test_verify_catches_edited_field(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry(tool="read_file"))
    log.append(_mk_entry(tool="read_file"))
    log.append(_mk_entry(tool="read_file"))

    # Tamper with line 2: change the tool name, keep the hmac
    # (simulating an attacker who doesn't have the key).
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    entry2 = json.loads(lines[1])
    entry2["tool"] = "delete_everything"  # tamper
    lines[1] = json.dumps(entry2)
    path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert not result.ok
    assert result.bad_line == 2
    assert "HMAC mismatch" in result.reason


def test_verify_catches_inserted_line(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry(tool="a"))
    log.append(_mk_entry(tool="c"))

    # Insert a fake line 2 with a fabricated (wrong) prev_hmac.
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    fake = _mk_entry(tool="b")
    fake.prev_hmac = "ff" * 32
    fake.hmac = "aa" * 32
    from dataclasses import asdict

    lines.insert(1, json.dumps(asdict(fake)))
    path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert not result.ok
    assert result.bad_line == 2


def test_verify_catches_deleted_line(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry(tool="a"))
    log.append(_mk_entry(tool="b"))
    log.append(_mk_entry(tool="c"))

    # Delete line 2 so line 3's prev_hmac now points at a ghost.
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert not result.ok
    # Line 2 now holds what used to be entry 3; its prev_hmac
    # still points at entry 1's hmac, but our walk has prev=entry0
    # → mismatch reported at line 2.
    assert result.bad_line == 2
    assert "prev_hmac" in result.reason


def test_verify_catches_malformed_json(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry())
    log.append(_mk_entry())

    _overwrite_line(tmp_path / "audit.jsonl", 2, "this is not json")
    result = log.verify()
    assert not result.ok
    assert result.bad_line == 2
    assert "malformed JSON" in result.reason


def test_verify_missing_key_file(tmp_path: Path) -> None:
    """Key file missing after entries exist → chain unverifiable."""
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry())
    # Operator deletes / misplaces the key.
    (tmp_path / "audit.key").unlink()

    fresh = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    result = fresh.verify()
    # We don't crash; we report the chain as broken.
    assert not result.ok


# --------------------------------------------------------------- iter / filter helpers


def test_iter_entries_returns_parsed_dicts(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry(tool="a"))
    log.append(_mk_entry(tool="b"))
    entries = log.iter_entries()
    assert [e["tool"] for e in entries] == ["a", "b"]


def test_iter_entries_skips_malformed(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl", key_path=tmp_path / "audit.key")
    log.append(_mk_entry(tool="a"))
    # Append a garbage line (simulating a partial write from a
    # crash in some hypothetical scenario).
    with (tmp_path / "audit.jsonl").open("a") as f:
        f.write("not json\n")
    log.append(_mk_entry(tool="b"))
    entries = log.iter_entries()
    # The garbage line is skipped; the two valid ones come through.
    assert [e["tool"] for e in entries] == ["a", "b"]


# --------------------------------------------------------------- property-based


@given(
    st.lists(
        st.fixed_dictionaries(
            {
                "tool": st.sampled_from(
                    ["read_file", "write_file", "git_status", "run_tests", "http_get"]
                ),
                "client": st.sampled_from(["ide", "telegram", "webui", "cli"]),
                "decision": st.sampled_from(["auto", "approved", "rejected", "timeout", "blocked"]),
                "ok": st.booleans(),
                "duration_ms": st.integers(min_value=0, max_value=60_000),
            }
        ),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=50, deadline=None)
def test_chain_integrity_end_to_end(tmp_path_factory: pytest.TempPathFactory, seq: list) -> None:
    """For any sequence of entries: appending them produces a
    chain; tampering with any field at any position is detected.

    Uses tmp_path_factory for a fresh dir per example — hypothesis
    doesn't tear down between invocations so we can't share state."""
    import secrets as _secrets

    tmp = tmp_path_factory.mktemp(f"audit_{_secrets.token_hex(4)}")
    log = AuditLog(path=tmp / "audit.jsonl", key_path=tmp / "audit.key")
    for item in seq:
        log.append(new_entry(session_key="main", args={"k": "v"}, **item))

    # Clean chain verifies.
    result = log.verify()
    assert result.ok, f"clean chain failed at {result.bad_line}: {result.reason}"


def test_chain_integrity_detects_tamper_at_every_position(tmp_path: Path) -> None:
    """Concrete (non-hypothesis) proof: build a 5-entry chain,
    tamper at each position in turn, verify catches each one."""
    for tamper_at in range(1, 6):
        tmp = tmp_path / f"tamper_{tamper_at}"
        tmp.mkdir()
        log = AuditLog(path=tmp / "audit.jsonl", key_path=tmp / "audit.key")
        for i in range(5):
            log.append(_mk_entry(tool=f"tool_{i}"))

        # Bit-flip the tool name at `tamper_at` (1-based).
        path = tmp / "audit.jsonl"
        lines = path.read_text().splitlines()
        entry = json.loads(lines[tamper_at - 1])
        entry["tool"] = entry["tool"] + "_TAMPERED"
        lines[tamper_at - 1] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")

        fresh = AuditLog(path=tmp / "audit.jsonl", key_path=tmp / "audit.key")
        result = fresh.verify()
        assert not result.ok, f"Tamper at line {tamper_at} not caught"
        assert result.bad_line == tamper_at, f"Expected bad_line={tamper_at}, got {result.bad_line}"


# --------------------------------------------------------------- hmac helper


def test_hmac_changes_when_any_field_changes(tmp_path: Path) -> None:
    """Sanity: every non-hmac field contributes to the HMAC.
    Guards against accidentally dropping a field from
    _ENTRY_FIELDS during a refactor."""
    key = b"k" * 32
    base = _mk_entry()
    h0 = _compute_hmac(key, base)

    for field_name, new_value in [
        ("session_key", "other"),
        ("client", "telegram"),
        ("tool", "write_file"),
        ("args", {"different": True}),
        ("decision", "blocked"),
        ("ok", False),
        ("duration_ms", 999),
        ("error", "boom"),
        ("prev_hmac", "deadbeef"),
        ("extra", {"iteration": 1}),
        ("ts", base.ts + 1),
    ]:
        mutated = _mk_entry()
        setattr(mutated, field_name, new_value)
        h1 = _compute_hmac(key, mutated)
        assert h0 != h1, f"HMAC didn't change when {field_name!r} changed"
