"""Shared structured argv and ``env`` wrapper validation for native collectors."""

from __future__ import annotations

from live_collectors.policy_data import policy_set

import json
import shlex
from pathlib import Path
from typing import Any, Callable, Sequence


ENV_NO_OPERAND_OPTIONS = policy_set("native_argv", "ENV_NO_OPERAND_OPTIONS")
ENV_OPERAND_OPTIONS = policy_set("native_argv", "ENV_OPERAND_OPTIONS")
ENV_OPERAND_PREFIXES = ("--unset=", "--chdir=", "--path=")
ENV_SPLIT_OPTIONS = {"-S", "--split-string"}
MAX_ENV_WRAPPER_DEPTH = 16


def executable_name(value: Any) -> str:
    name = Path(str(value or "").strip().strip("'\"")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def is_env_assignment(value: str) -> bool:
    return bool(value and "=" in value and not value.startswith("-")
                and value.split("=", 1)[0].isidentifier())


def split_env_string(raw: str) -> tuple[list[str], str]:
    try:
        return shlex.split(raw), ""
    except ValueError as exc:
        return [], f"env split string is malformed: {exc}"


def unwrap_env_command(
    tokens: Sequence[str],
    *,
    depth: int = 0,
) -> tuple[list[str], str]:
    if depth > MAX_ENV_WRAPPER_DEPTH:
        return [], "env wrapper chain is too deep"
    items = [str(item) for item in tokens]
    idx = 1 if items and executable_name(items[0]) == "env" else 0
    while idx < len(items):
        item = items[idx]
        if item == "--":
            idx += 1
            break
        if is_env_assignment(item):
            idx += 1
            continue
        split_value = ""
        if item in ENV_SPLIT_OPTIONS:
            if idx + 1 >= len(items):
                return [], "env split string is missing"
            split_value = items[idx + 1]
            trailing = idx + 2 < len(items)
        elif item.startswith("--split-string=") or item.startswith("-S") and item != "-S":
            trailing = idx + 1 < len(items)
            split_value = item.split("=", 1)[1] if "=" in item else item[2:]
        if split_value:
            if trailing:
                return [], "env split string has ambiguous trailing arguments"
            split_tokens, error = split_env_string(split_value)
            if error:
                return [], error
            if not split_tokens:
                return [], "env split string did not contain a command"
            return unwrap_env_command(split_tokens, depth=depth + 1)
        operand_option = item in ENV_OPERAND_OPTIONS
        attached_operand = item.startswith(ENV_OPERAND_PREFIXES) or (
            item.startswith(("-P", "-u", "-C"))
            and len(item) > 2
        )
        if operand_option or attached_operand:
            if operand_option and idx + 1 >= len(items):
                return [], f"env option `{item}` is missing its operand"
            idx += 2 if operand_option else 1
            continue
        if item.startswith("-"):
            if item not in ENV_NO_OPERAND_OPTIONS:
                return [], f"env option `{item}` is not allowed"
            idx += 1
            continue
        break
    if idx >= len(items):
        return [], "env wrapper must include a command"
    target = items[idx:]
    if executable_name(target[0]) == "env":
        return unwrap_env_command(target, depth=depth + 1)
    return target, ""


def env_shell_target(
    argv: Sequence[str],
    *,
    shell_names: set[str],
) -> tuple[str, str]:
    if not argv or executable_name(argv[0]) != "env":
        return "", ""
    target, error = unwrap_env_command(argv)
    shell = target[0] if target and executable_name(target[0]) in shell_names else ""
    return ("", error) if error else (shell, "")


def validate_env_wrapper(
    argv: Sequence[str],
    label: str,
    *,
    shell_names: set[str],
    make_problem: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    target, error = env_shell_target(argv, shell_names=shell_names)
    if error:
        return [make_problem(f"{label} {error}")]
    return [make_problem(
        f"{label} argv must not invoke shell `{executable_name(target)}` through env"
    )] if target else []


def parse_argv_json(
    raw: str | None,
    label: str,
    *,
    required: bool,
    validate: Callable[[Sequence[str], str], list[dict[str, Any]]],
    make_problem: Callable[[str], dict[str, Any]],
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    text = str(raw or "").strip()
    if not text:
        return (
            None,
            [make_problem(f"{label} argv is required")] if required else [],
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [make_problem(f"{label} argv must be a JSON array: {exc}")]
    if not isinstance(parsed, list):
        return None, [make_problem(f"{label} argv must be a JSON array")]
    for idx, item in enumerate(parsed):
        message = (
            "must be a non-empty string" if not isinstance(item, str) or not item
            else "contains a null byte" if "\0" in item else ""
        )
        if message:
            return None, [make_problem(f"{label} argv item {idx + 1} {message}")]
    problems = validate(parsed, label)
    return (parsed if not problems else None), problems
