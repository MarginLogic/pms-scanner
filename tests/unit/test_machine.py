"""Unit tests for scanner/machine.py — T005 (004 foundational).

Covers MachineIdentity validation (valid names, reserved-name rejection,
blank/whitespace rejection, regex constraints) and the in_progress_dir(env)
path-resolution helper.
"""
from pathlib import Path

import pytest
from machine import InvalidMachineIdentityError, MachineIdentity


class _FakeEnv:
    """Minimal stand-in for config.Environment exposing in_progress_dir."""

    def __init__(self, watch_dir: Path) -> None:
        self.watch_dir = watch_dir

    def in_progress_dir(self, machine: MachineIdentity) -> Path:
        return self.watch_dir / "in-progress" / machine.name


@pytest.mark.parametrize("name", ["macmini", "nuc", "lab-01", "host_2", "a", "0"])
def test_valid_names_accepted(name: str) -> None:
    assert MachineIdentity(name).name == name


def test_surrounding_whitespace_is_stripped() -> None:
    assert MachineIdentity("  macmini  ").name == "macmini"


@pytest.mark.parametrize("name", ["in-progress", "processed", "..", "."])
def test_reserved_names_rejected(name: str) -> None:
    with pytest.raises(InvalidMachineIdentityError):
        MachineIdentity(name)


@pytest.mark.parametrize("name", ["", "   ", "\t", "\n "])
def test_blank_or_whitespace_rejected(name: str) -> None:
    with pytest.raises(InvalidMachineIdentityError):
        MachineIdentity(name)


@pytest.mark.parametrize(
    "name",
    [
        "Macmini",          # uppercase not allowed
        "-leading-dash",    # must start with [a-z0-9]
        "_leading_us",      # must start with [a-z0-9]
        "has space",        # space illegal
        "has/slash",        # path separator illegal
        "has.dot",          # dot illegal in body
        "x" * 32,           # 32 chars: 1 + 31 > 30 max tail
    ],
)
def test_regex_constraints_rejected(name: str) -> None:
    with pytest.raises(InvalidMachineIdentityError):
        MachineIdentity(name)


def test_max_length_boundary_accepted() -> None:
    # 1 leading char + 30 tail chars = 31 total is the inclusive maximum.
    name = "a" + ("b" * 30)
    assert MachineIdentity(name).name == name


def test_in_progress_dir_resolves_under_env_watch_dir(tmp_path: Path) -> None:
    machine = MachineIdentity("macmini")
    env = _FakeEnv(tmp_path)
    assert machine.in_progress_dir(env) == tmp_path / "in-progress" / "macmini"


def test_machine_identity_is_hashable_and_frozen() -> None:
    m = MachineIdentity("nuc")
    {m}  # hashable
    with pytest.raises(Exception):
        m.name = "other"  # type: ignore[misc]
