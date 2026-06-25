"""Tests for the watchdog scope (deny pattern) filter."""

from __future__ import annotations

import pytest

from baird.scope import ScopeFilter

DEFAULTS = [
    "**/.git/**",
    "**/.snakemake/**",
    "**/.nextflow/**",
    "**/__pycache__/**",
    "**/conda-envs/**",
    "**/.ipynb_checkpoints/**",
    "**/*.swp",
    "**/*.tmp",
    "**/tmp/**",
]


@pytest.fixture
def scope() -> ScopeFilter:
    return ScopeFilter(DEFAULTS)


def test_allows_normal_source_file(scope: ScopeFilter) -> None:
    assert not scope.is_denied("project/src/main.py")
    assert not scope.is_denied("notebooks/exploration.ipynb")
    assert not scope.is_denied("data/raw/counts.h5")


def test_denies_git_objects(scope: ScopeFilter) -> None:
    assert scope.is_denied("project/.git/objects/ab/cdef")
    assert scope.is_denied(".git/HEAD")


def test_denies_snakemake_shadow(scope: ScopeFilter) -> None:
    assert scope.is_denied("project/.snakemake/log/run.log")


def test_denies_pycache(scope: ScopeFilter) -> None:
    assert scope.is_denied("project/pkg/__pycache__/module.cpython-311.pyc")


def test_denies_swap_files(scope: ScopeFilter) -> None:
    assert scope.is_denied(".main.py.swp")
    assert scope.is_denied("project/.main.py.swp")


def test_denies_tmp_extension(scope: ScopeFilter) -> None:
    assert scope.is_denied("output.tmp")
    assert scope.is_denied("project/intermediate.tmp")


def test_denies_tmp_directory(scope: ScopeFilter) -> None:
    assert scope.is_denied("project/tmp/working.txt")
    assert scope.is_denied("tmp/x")


def test_empty_patterns_allows_everything() -> None:
    s = ScopeFilter([])
    assert not s.is_denied("anything")
    assert not s.is_denied(".git/HEAD")


def test_handles_windows_style_paths(scope: ScopeFilter) -> None:
    # Just to be safe — internal normalization
    assert scope.is_denied("project\\.git\\HEAD")
