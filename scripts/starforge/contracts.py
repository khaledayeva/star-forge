"""Versioned project contracts used by the Star Forge lifecycle."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping


BLUEPRINT_FILE = "Blueprint.md"
BLUEPRINT_LOCK_FILE = "Blueprint.lock.json"
BLUEPRINT_LOCK_SCHEMA = "star-forge.blueprint-lock.v1"
BLUEPRINT_CONTRACT_VERSION = 1

_LOCK_FIELDS = frozenset(
    {
        "schema",
        "blueprint_sha256",
        "approved_at",
        "contract_version",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """A project contract could not be read or safely updated."""


def _now_utc() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _regular_file_problem(path: Path, *, required: bool) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return f"{path.name} is missing" if required else ""
    except OSError:
        return f"{path.name} cannot be inspected"
    if stat.S_ISLNK(mode):
        return f"{path.name} must not be a symlink"
    if not stat.S_ISREG(mode):
        return f"{path.name} must be a regular file"
    if mode & 0o444 == 0:
        return f"{path.name} is not readable"
    return ""


def blueprint_sha256(path: Path) -> str:
    """Hash the exact Blueprint bytes without normalizing its content."""
    problem = _regular_file_problem(path, required=True)
    if problem:
        raise ContractError(problem)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"{path.name} cannot be read: {exc}") from exc
    return digest.hexdigest()


def blueprint_text_has_legacy_approval(text: str) -> bool:
    """Recognize the mutable v0.3 approval sentinel for compatibility only."""
    normalized = re.sub(r"\*\*|__", "", text)
    if re.search(
        r"^\s*Status\s*[:\-—]\s*approved\b",
        normalized,
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    match = re.search(
        r"^\s*Last approved\s*[:\-—]\s*(.+)$",
        normalized,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}\b", match.group(1).strip()))


def _valid_iso8601(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        )
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_blueprint_lock(payload: Any) -> list[str]:
    """Return deterministic validation problems for a v1 lock payload."""
    if not isinstance(payload, Mapping):
        return [f"{BLUEPRINT_LOCK_FILE} must contain a JSON object"]
    problems: list[str] = []
    payload_fields = {str(key) for key in payload}
    missing = sorted(_LOCK_FIELDS - payload_fields)
    extra = sorted(payload_fields - _LOCK_FIELDS)
    if missing:
        problems.append("missing fields: " + ", ".join(missing))
    if extra:
        problems.append("unexpected fields: " + ", ".join(extra))
    if payload.get("schema") != BLUEPRINT_LOCK_SCHEMA:
        problems.append(f"schema must be {BLUEPRINT_LOCK_SCHEMA}")
    digest = payload.get("blueprint_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        problems.append("blueprint_sha256 must be a lowercase SHA-256 digest")
    if not _valid_iso8601(payload.get("approved_at")):
        problems.append("approved_at must be an ISO-8601 timestamp with a timezone")
    version = payload.get("contract_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != BLUEPRINT_CONTRACT_VERSION
    ):
        problems.append(
            f"contract_version must be {BLUEPRINT_CONTRACT_VERSION}"
        )
    return problems


def _read_lock(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    problem = _regular_file_problem(path, required=False)
    if problem:
        return None, [problem]
    if not path.exists():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return None, [f"{BLUEPRINT_LOCK_FILE} is not valid UTF-8"]
    except json.JSONDecodeError:
        return None, [f"{BLUEPRINT_LOCK_FILE} is not valid JSON"]
    except OSError:
        return None, [f"{BLUEPRINT_LOCK_FILE} cannot be read"]
    problems = validate_blueprint_lock(payload)
    return (dict(payload) if isinstance(payload, Mapping) else None), problems


def blueprint_lock_state(project: Path) -> dict[str, Any]:
    """Describe v0.4 lock validity and legacy approval without conflating them."""
    root = project.resolve()
    blueprint_path = root / BLUEPRINT_FILE
    lock_path = root / BLUEPRINT_LOCK_FILE
    blueprint_problem = _regular_file_problem(blueprint_path, required=True)
    lock, lock_problems = _read_lock(lock_path)

    current_sha256: str | None = None
    legacy_approved = False
    if not blueprint_problem:
        try:
            text = blueprint_path.read_text(encoding="utf-8")
            current_sha256 = blueprint_sha256(blueprint_path)
            legacy_approved = blueprint_text_has_legacy_approval(text)
        except UnicodeDecodeError:
            blueprint_problem = f"{BLUEPRINT_FILE} is not valid UTF-8"
        except ContractError as exc:
            blueprint_problem = str(exc)
        except OSError:
            blueprint_problem = f"{BLUEPRINT_FILE} cannot be read"

    problems = ([blueprint_problem] if blueprint_problem else []) + lock_problems
    lock_exists = lock_path.exists() or bool(lock_problems)
    status: str
    if blueprint_problem:
        status = "invalid" if lock_exists else "missing"
    elif lock_problems:
        status = "invalid"
    elif lock is not None and lock.get("blueprint_sha256") != current_sha256:
        status = "drifted"
        problems.append(
            f"{BLUEPRINT_FILE} does not match {BLUEPRINT_LOCK_FILE}"
        )
    elif lock is not None:
        status = "locked"
    elif legacy_approved:
        status = "legacy-approved"
    else:
        status = "draft"

    locked = status == "locked"
    return {
        "schema": "star-forge.blueprint-lock-state.v1",
        "path": BLUEPRINT_FILE,
        "lock_path": BLUEPRINT_LOCK_FILE,
        "status": status,
        "approved": locked or status == "legacy-approved",
        "locked": locked,
        "legacy_approved": status == "legacy-approved",
        "current_sha256": current_sha256,
        "locked_sha256": lock.get("blueprint_sha256") if lock else None,
        "approved_at": lock.get("approved_at") if lock else None,
        "contract_version": lock.get("contract_version") if lock else None,
        "problems": problems,
    }


def write_blueprint_lock(
    project: Path,
    *,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Atomically approve the current Blueprint without editing its source."""
    root = project.resolve()
    blueprint_path = root / BLUEPRINT_FILE
    lock_path = root / BLUEPRINT_LOCK_FILE
    lock_problem = _regular_file_problem(lock_path, required=False)
    if lock_problem:
        raise ContractError(lock_problem)
    timestamp = approved_at or _now_utc()
    payload: dict[str, Any] = {
        "schema": BLUEPRINT_LOCK_SCHEMA,
        "blueprint_sha256": blueprint_sha256(blueprint_path),
        "approved_at": timestamp,
        "contract_version": BLUEPRINT_CONTRACT_VERSION,
    }
    problems = validate_blueprint_lock(payload)
    if problems:
        raise ContractError("; ".join(problems))

    root.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".Blueprint.lock.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, lock_path)
    except OSError as exc:
        raise ContractError(
            f"Could not write {BLUEPRINT_LOCK_FILE}: {exc}"
        ) from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return payload
