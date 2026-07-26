"""Readable declarative validation primitives shared by runtime contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def flag(problems: list[str], condition: bool, message: str) -> None:
    if condition:
        problems.append(message)


def rules(problems: list[str], *checks: tuple[bool, str]) -> None:
    """Append messages for failed declarative predicates in declaration order."""
    problems.extend(message for failed, message in checks if failed)


def mapping_sections(
    payload: Mapping[str, Any],
    names: Sequence[str],
    problems: list[str],
) -> dict[str, Mapping[str, Any]]:
    sections: dict[str, Mapping[str, Any]] = {}
    for name in names:
        value = payload.get(name)
        flag(problems, not isinstance(value, Mapping), f"{name} must be an object")
        sections[name] = value if isinstance(value, Mapping) else {}
    return sections


def boolean_fields(
    payload: Mapping[str, Any],
    names: Sequence[str],
    problems: list[str],
    *,
    prefix: str = "",
) -> None:
    for name in names:
        flag(problems, not isinstance(payload.get(name), bool), f"{prefix}{name} must be boolean")
