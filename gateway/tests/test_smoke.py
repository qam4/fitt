"""Smoke tests: does the package import, is the version sensible."""

from __future__ import annotations


def test_package_imports() -> None:
    import gateway

    assert gateway.__version__
    # Basic version format: major.minor.patch
    parts = gateway.__version__.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts[:2])
