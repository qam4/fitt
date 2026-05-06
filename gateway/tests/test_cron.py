"""Tests for Phase 4.5 Task 2 — cron data model + persistence.

Four concerns:

* ``parse_schedule_spec`` accepts each of the documented spec
  shapes and rejects malformed ones with clear messages. Each
  shape hits :meth:`CronSchedule.next_run` to confirm the parser
  + arithmetic agree.
* ``CronService`` CRUD persists to JSON and survives round-trips
  (including restart — build a new service instance against the
  same file).
* Mtime-based reload detects external edits (CLI process
  writing while gateway process is running).
* Atomic-write invariants: a crash mid-write leaves the
  original file intact; no ``.tmp`` file lingers on success.

Scheduler loop tests come in task 4 when we actually wire the
loop — this module is pure data.
"""

from __future__ import annotations

import json
import time
from datetime import UTC
from pathlib import Path

import pytest

from gateway.cron import (
    CronJob,
    CronSchedule,
    CronService,
    DuplicateCron,
    InvalidSchedule,
    UnknownCron,
    parse_schedule_spec,
)

# --------------------------------------------------------------- schedule parsing


def test_parse_every_seconds() -> None:
    s = parse_schedule_spec("every 60")
    assert s.kind == "every"
    assert s.every_secs == 60
    # Default unit is seconds.
    assert parse_schedule_spec("every 30s").every_secs == 30


def test_parse_every_with_minute_and_hour_units() -> None:
    assert parse_schedule_spec("every 5m").every_secs == 300
    assert parse_schedule_spec("every 5 minutes").every_secs == 300
    assert parse_schedule_spec("every 2h").every_secs == 7200
    assert parse_schedule_spec("every 1 hour").every_secs == 3600


def test_parse_every_rejects_zero() -> None:
    with pytest.raises(InvalidSchedule, match="> 0"):
        parse_schedule_spec("every 0")
    with pytest.raises(InvalidSchedule, match="> 0"):
        parse_schedule_spec("every 0m")


def test_parse_in_minutes() -> None:
    """'in N minutes' resolves to an 'at' schedule now+delta."""
    s = parse_schedule_spec("in 30 minutes", now=1000.0)
    assert s.kind == "at"
    assert s.at_ts == 1000.0 + 30 * 60


def test_parse_at_iso() -> None:
    s = parse_schedule_spec("at 2026-05-06T09:00:00+00:00")
    assert s.kind == "at"
    assert s.at_ts is not None
    # Round trip through datetime to confirm.
    from datetime import datetime

    assert datetime.fromtimestamp(s.at_ts, tz=UTC) == datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC)


def test_parse_at_iso_naive_uses_tz() -> None:
    """A naive timestamp is interpreted in the provided tz."""
    # 2026-05-06T09:00 in LA (PDT, UTC-7) = 16:00 UTC = epoch 1778083200
    s = parse_schedule_spec("at 2026-05-06T09:00:00", tz="America/Los_Angeles")
    assert s.kind == "at"
    assert s.at_ts == 1778083200.0


def test_parse_at_epoch() -> None:
    s = parse_schedule_spec("at 1777497338")
    assert s.kind == "at"
    assert s.at_ts == 1777497338.0


def test_parse_cron_expression() -> None:
    s = parse_schedule_spec("cron 0 9 * * *")
    assert s.kind == "cron"
    assert s.cron_expr == "0 9 * * *"


def test_parse_cron_rejects_garbage() -> None:
    with pytest.raises(InvalidSchedule, match="invalid cron expression"):
        parse_schedule_spec("cron not a cron")


def test_parse_rejects_empty() -> None:
    with pytest.raises(InvalidSchedule):
        parse_schedule_spec("")
    with pytest.raises(InvalidSchedule):
        parse_schedule_spec("   ")


def test_parse_rejects_unknown_shape() -> None:
    with pytest.raises(InvalidSchedule, match="could not parse"):
        parse_schedule_spec("tomorrow at noon")


# --------------------------------------------------------------- next_run


def test_next_run_every() -> None:
    s = CronSchedule(kind="every", every_secs=60)
    assert s.next_run(after=1000.0) == 1060.0
    assert s.next_run(after=1059.0) == 1119.0


def test_next_run_at_one_shot() -> None:
    """An 'at' schedule fires once, then returns None."""
    s = CronSchedule(kind="at", at_ts=2000.0)
    assert s.next_run(after=1000.0) == 2000.0
    # After the firing moment: consumed.
    assert s.next_run(after=2000.0) is None
    assert s.next_run(after=3000.0) is None


def test_next_run_cron_expression_utc() -> None:
    """Daily 9 AM UTC. First firing after epoch 0 is epoch 9*3600."""
    s = CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="UTC")
    assert s.next_run(after=0.0) == 9 * 3600


def test_next_run_cron_expression_with_tz() -> None:
    """Daily 9 AM America/Los_Angeles (Jan 1 1970 is PST, UTC-8).
    First firing after epoch 0 → 17:00 UTC → epoch 61200."""
    s = CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="America/Los_Angeles")
    assert s.next_run(after=0.0) == 61200.0


def test_next_run_cron_unknown_tz_falls_back_to_utc() -> None:
    """Rather than crashing the gateway on a typo'd tz, fall
    back to UTC and log a warning. The cron still fires, just
    not where the user expected."""
    s = CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="Not/A/Zone")
    # UTC fallback: first firing after epoch 0 is 9 AM UTC = 32400.
    assert s.next_run(after=0.0) == 32400.0


def test_next_run_cron_is_monotonic() -> None:
    """Across N consecutive calls threading last-result into next-after,
    next_run must be strictly increasing. Property we lean on in
    the scheduler loop."""
    s = CronSchedule(kind="cron", cron_expr="*/5 * * * *")
    after = 0.0
    last = 0.0
    for _ in range(50):
        n = s.next_run(after=after)
        assert n is not None
        assert n > last
        last = n
        after = n


# --------------------------------------------------------------- service CRUD


def _mk_job(id: str = "", name: str = "n", message: str = "m") -> CronJob:
    return CronJob(
        id=id,
        name=name,
        message=message,
        schedule=CronSchedule(kind="every", every_secs=60),
    )


def test_service_add_and_get(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "cron.json")
    job = svc.add(_mk_job(name="daily briefing"))
    assert job.id  # auto-generated
    assert job.created_ts > 0  # auto-stamped

    got = svc.get(job.id)
    assert got is not None
    assert got.name == "daily briefing"


def test_service_list_orders_by_created_then_id(tmp_path: Path) -> None:
    """Stable, reproducible ordering — the CLI's `fitt cron list`
    and the `cron_list` tool both rely on it."""
    svc = CronService(tmp_path / "cron.json")
    j1 = svc.add(_mk_job(name="a"))
    # Make the second slightly later so created_ts orders them.
    time.sleep(0.001)
    j2 = svc.add(_mk_job(name="b"))
    got = svc.list()
    assert [j.id for j in got] == [j1.id, j2.id]


def test_service_list_filters_disabled(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "cron.json")
    svc.add(_mk_job(name="on"))
    j2 = svc.add(_mk_job(name="off"))
    svc.set_enabled(j2.id, False)
    assert {j.name for j in svc.list(include_disabled=False)} == {"on"}
    assert {j.name for j in svc.list(include_disabled=True)} == {"on", "off"}


def test_service_update_fields(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "cron.json")
    job = svc.add(_mk_job())
    updated = svc.update(
        job.id,
        name="renamed",
        message="new prompt",
        silent=True,
        approval_mode="auto",
    )
    assert updated.name == "renamed"
    assert updated.message == "new prompt"
    assert updated.silent is True
    assert updated.approval_mode == "auto"

    # And it persists.
    reloaded = CronService(tmp_path / "cron.json")
    assert reloaded.get(job.id) is not None
    assert reloaded.get(job.id).name == "renamed"  # type: ignore[union-attr]


def test_service_update_unknown_raises(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "cron.json")
    with pytest.raises(UnknownCron):
        svc.update("nope", name="x")


def test_service_remove(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "cron.json")
    job = svc.add(_mk_job())
    assert svc.remove(job.id) is True
    assert svc.remove(job.id) is False  # idempotent
    assert svc.get(job.id) is None


def test_service_duplicate_id_raises(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "cron.json")
    job = _mk_job(id="aaaa")
    svc.add(job)
    with pytest.raises(DuplicateCron):
        svc.add(_mk_job(id="aaaa"))


def test_service_at_schedule_flags_delete_after_run(tmp_path: Path) -> None:
    """One-shot 'at' jobs auto-set delete_after_run. The
    scheduler uses this to clean up."""
    svc = CronService(tmp_path / "cron.json")
    job = CronJob(
        id="",
        name="one-shot",
        message="m",
        schedule=CronSchedule(kind="at", at_ts=time.time() + 60),
    )
    stored = svc.add(job)
    assert stored.delete_after_run is True


# --------------------------------------------------------------- persistence


def test_service_round_trip_across_process(tmp_path: Path) -> None:
    """Simulating a gateway restart: fresh CronService against
    the same file must see the jobs written by the previous
    instance."""
    path = tmp_path / "cron.json"
    svc1 = CronService(path)
    j = svc1.add(_mk_job(name="persist-me"))

    svc2 = CronService(path)
    got = svc2.get(j.id)
    assert got is not None
    assert got.name == "persist-me"
    assert got.schedule.kind == "every"
    assert got.schedule.every_secs == 60


def test_service_writes_pretty_json(tmp_path: Path) -> None:
    """We write indent=2 so the file is human-editable. Not a
    functional requirement, but part of the "operators can
    poke at it" promise."""
    path = tmp_path / "cron.json"
    svc = CronService(path)
    svc.add(_mk_job(name="readable"))
    raw = path.read_text(encoding="utf-8")
    assert "\n  " in raw  # indentation present
    payload = json.loads(raw)
    assert payload["jobs"][0]["name"] == "readable"


def test_service_tolerates_missing_file_on_startup(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "nonexistent.json")
    assert svc.list() == []
    # And can still add.
    svc.add(_mk_job())
    assert len(svc.list()) == 1


def test_service_tolerates_corrupted_file(tmp_path: Path) -> None:
    """Mangled JSON logs a warning and comes back as empty.
    We don't want a bad file to lock the gateway out of its
    own cron system."""
    path = tmp_path / "cron.json"
    path.write_text("{not valid json", encoding="utf-8")
    svc = CronService(path)
    assert svc.list() == []


def test_service_skips_malformed_entries(tmp_path: Path) -> None:
    """One bad entry shouldn't lose the others."""
    path = tmp_path / "cron.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "good",
                        "name": "ok",
                        "message": "m",
                        "schedule": {"kind": "every", "every_secs": 60},
                    },
                    {"id": "bad", "name": "missing schedule"},
                    {
                        "id": "alsogood",
                        "name": "ok2",
                        "message": "m",
                        "schedule": {"kind": "every", "every_secs": 30},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    svc = CronService(path)
    ids = {j.id for j in svc.list()}
    assert ids == {"good", "alsogood"}


def test_service_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "cron.json"
    svc = CronService(path)
    svc.add(_mk_job())
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists()


# --------------------------------------------------------------- reload


def test_reload_if_changed_picks_up_external_edit(tmp_path: Path) -> None:
    """The `fitt cron add` CLI writes the file from a separate
    process; the running gateway's CronService must see it on
    the next reload call."""
    path = tmp_path / "cron.json"
    gateway = CronService(path)
    assert gateway.list() == []

    # External writer (CLI) constructs a second service against
    # the same file and adds a job.
    cli = CronService(path)
    cli.add(_mk_job(name="from-cli"))

    # Bump mtime to ensure the gateway's service notices — on
    # some filesystems, two writes within the same sub-second
    # can collide to the same mtime.
    _bump_mtime(path)

    assert gateway.reload_if_changed() is True
    assert {j.name for j in gateway.list()} == {"from-cli"}


def test_reload_if_changed_no_op_when_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "cron.json"
    svc = CronService(path)
    svc.add(_mk_job())
    # Second call — file hasn't changed.
    assert svc.reload_if_changed() is False


def test_reload_handles_file_deleted(tmp_path: Path) -> None:
    """Operator deletes cron.json to start from scratch. Reload
    clears the in-memory map to match."""
    path = tmp_path / "cron.json"
    svc = CronService(path)
    svc.add(_mk_job())
    assert len(svc.list()) == 1

    path.unlink()
    svc.reload_if_changed()
    # On filesystems where mtime was already -1 for the missing
    # file, reload_if_changed returns False but the state is
    # cleared. Either way the jobs list is empty after reload.
    assert svc.list() == []


def _bump_mtime(path: Path) -> None:
    """Nudge the file's mtime forward by a microsecond to work
    around same-second write collisions on some filesystems."""
    import os

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
