"""Tests for the project registry.

Covers schema validation, round-trip through YAML, atomic writes,
and error cases. Malformed files should degrade gracefully rather
than crashing the gateway.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from gateway.projects import (
    DuplicateProject,
    InvalidProjectName,
    InvalidProjectPath,
    Project,
    ProjectRegistry,
    UnknownProject,
    default_projects_path,
)

# ----------------------------------------------------------------- helpers


def _registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(config_path=tmp_path / "projects.yaml")


def _sample_project(
    name: str = "home-ai-cluster",
    path: str = "/share/Public/home-ai-cluster",
    ssh_host: str = "",
    test_command: str = "",
    build_command: str = "",
) -> Project:
    return Project(
        name=name,
        path=path,
        ssh_host=ssh_host,
        test_command=test_command,
        build_command=build_command,
    )


# ----------------------------------------------------------------- basic CRUD


def test_registry_is_empty_when_file_missing(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.all() == []
    assert reg.known_names() == []


def test_ensure_exists_creates_empty_file(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.ensure_exists()
    assert (tmp_path / "projects.yaml").exists()
    assert reg.all() == []


def test_ensure_exists_is_idempotent(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.ensure_exists()
    reg.add(_sample_project())
    reg.ensure_exists()  # should not clobber
    assert reg.known_names() == ["home-ai-cluster"]


def test_add_project_persists(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project())

    # New instance, same file: project is still there.
    fresh = _registry(tmp_path)
    assert [p.name for p in fresh.all()] == ["home-ai-cluster"]


def test_add_duplicate_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project())
    with pytest.raises(DuplicateProject):
        reg.add(_sample_project())


def test_get_unknown_raises_with_available_list(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project(name="a"))
    reg.add(_sample_project(name="b"))

    with pytest.raises(UnknownProject) as exc:
        reg.get("does-not-exist")

    assert exc.value.name == "does-not-exist"
    assert exc.value.available == ["a", "b"]


def test_update_changes_fields(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project(test_command="pytest -q"))

    reg.update(
        "home-ai-cluster",
        test_command="uv run pytest -q",
        ssh_host="satellite.tailnet",
    )

    updated = reg.get("home-ai-cluster")
    assert updated.test_command == "uv run pytest -q"
    assert updated.ssh_host == "satellite.tailnet"
    assert updated.is_local is False


def test_update_unknown_project_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(UnknownProject):
        reg.update("nope", path="/whatever")


def test_update_ignores_unknown_field(tmp_path: Path, caplog) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project())
    reg.update("home-ai-cluster", unknown_field="value")
    assert "unknown_field" in caplog.text


def test_remove_project(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project(name="a"))
    reg.add(_sample_project(name="b"))
    reg.remove("a")
    assert reg.known_names() == ["b"]


def test_remove_unknown_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(UnknownProject):
        reg.remove("nope")


# ----------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "-leading-hyphen",
        ".leading-dot",
        "Space Not Allowed",
        "UPPERCASE",
        "contains/slash",
        "contains:colon",
        "contains@at",
    ],
)
def test_invalid_name_rejected(tmp_path: Path, bad_name: str) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(InvalidProjectName):
        reg.add(_sample_project(name=bad_name))


@pytest.mark.parametrize(
    "good_name",
    [
        "simple",
        "with-hyphens",
        "with_underscores",
        "with.dots",
        "a1b2",
        "home-ai-cluster",
        "a",
    ],
)
def test_valid_names_accepted(tmp_path: Path, good_name: str) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project(name=good_name))
    assert reg.get(good_name).name == good_name


def test_name_length_cap(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    # 64 chars = allowed; 65 = rejected
    reg.add(_sample_project(name="a" * 64))
    with pytest.raises(InvalidProjectName):
        reg.add(_sample_project(name="a" * 65))


def test_empty_path_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(InvalidProjectPath):
        reg.add(_sample_project(path=""))
    with pytest.raises(InvalidProjectPath):
        reg.add(_sample_project(path="   "))


# ----------------------------------------------------------------- is_local


def test_is_local_when_ssh_host_empty() -> None:
    p = _sample_project(ssh_host="")
    assert p.is_local is True


def test_not_local_when_ssh_host_set() -> None:
    p = _sample_project(ssh_host="laptop.tailnet")
    assert p.is_local is False


# ----------------------------------------------------------------- YAML parsing


def test_loads_from_handwritten_yaml(tmp_path: Path) -> None:
    content = dedent(
        """
        version: 1
        projects:
          - name: home-ai-cluster
            path: /share/Public/home-ai-cluster
            ssh_host: ""
            test_command: "cd gateway && uv run pytest -q"
            build_command: ""
          - name: retro-ai
            path: /home/fred/code/retro-ai
            ssh_host: laptop.tailnet
            test_command: pytest -q
            build_command: ""
        """
    ).strip()
    path = tmp_path / "projects.yaml"
    path.write_text(content, encoding="utf-8")

    reg = ProjectRegistry(config_path=path)
    projects = reg.all()
    assert [p.name for p in projects] == ["home-ai-cluster", "retro-ai"]
    assert projects[0].is_local is True
    assert projects[1].is_local is False
    assert projects[1].ssh_host == "laptop.tailnet"


def test_malformed_yaml_degrades_to_empty(tmp_path: Path, caplog) -> None:
    path = tmp_path / "projects.yaml"
    path.write_text("this: is: not: valid: yaml:", encoding="utf-8")
    reg = ProjectRegistry(config_path=path)
    assert reg.all() == []
    assert "projects.read_failed" in caplog.text


def test_malformed_entries_skipped(tmp_path: Path, caplog) -> None:
    content = dedent(
        """
        version: 1
        projects:
          - name: ok
            path: /tmp/ok
          - not-a-dict
          - name: bad name with spaces
            path: /tmp/bad
          - name: good
            path: /tmp/good
        """
    ).strip()
    path = tmp_path / "projects.yaml"
    path.write_text(content, encoding="utf-8")
    reg = ProjectRegistry(config_path=path)

    names = [p.name for p in reg.all()]
    assert names == ["good", "ok"]  # sorted
    assert "projects.entry_skip" in caplog.text


def test_missing_projects_key_is_fine(tmp_path: Path) -> None:
    path = tmp_path / "projects.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    reg = ProjectRegistry(config_path=path)
    assert reg.all() == []


# ----------------------------------------------------------------- atomic write


def test_atomic_write_no_tmp_leftover_on_success(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add(_sample_project())

    tmp_files = list(tmp_path.glob("projects-*.yaml.tmp"))
    assert tmp_files == []


# ----------------------------------------------------------------- default path


def test_default_path_respects_env(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "alt.yaml"
    monkeypatch.setenv("FITT_PROJECTS_PATH", str(custom))
    assert default_projects_path() == custom


def test_default_path_falls_back_to_fitt_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FITT_PROJECTS_PATH", raising=False)
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    assert default_projects_path() == tmp_path / "projects.yaml"
