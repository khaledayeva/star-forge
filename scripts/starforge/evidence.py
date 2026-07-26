"""Versioned, source-bound evidence envelopes for Star Forge."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


EVIDENCE_SCHEMA = "star-forge.evidence-envelope.v2"
LEGACY_LIVE_MANIFEST_SCHEMA = "star-forge.live-manifest.v1"
EVIDENCE_VERDICTS = frozenset({"PASS", "FAIL", "DEGRADED"})
REQUIRED_FIELDS = (
    "schema",
    "kind",
    "task",
    "capability",
    "provider",
    "provenance",
    "source_hash",
    "runtime_asset_hash",
    "started_at",
    "finished_at",
    "artifacts",
    "verdict",
    "blockers",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"\bsk-[A-Za-z0-9_-]{20,}|"
    r"\bAKIA[A-Z0-9]{16}\b|"
    r"\bghp_[A-Za-z0-9_]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}|"
    r"\bnpm_[A-Za-z0-9]{20,}|"
    r"\bpypi-[A-Za-z0-9_-]{20,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"BEGIN [A-Z ]*PRIVATE KEY|"
    r"\bAuthorization\s*[:=]\s*(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|AWS_SECRET_ACCESS_KEY)"
    r"\s*=\s*['\"]?[A-Za-z0-9_./+=:@-]{12,}|"
    r"\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s@/]{8,}@"
    r")",
    re.IGNORECASE,
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|credential|password|passwd|secret|token|api[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SAFE_PLACEHOLDERS = {
    "",
    "[redacted]",
    "[redacted_secret]",
    "<redacted>",
    "<secret>",
    "placeholder",
    "example",
    "not-set",
    "none",
}
_CAPABILITY_BY_COLLECTOR = {
    "browser": "local-web-qa",
    "browser-playwright": "local-web-qa",
    "preview": "preview-verification",
    "native-ios": "native-ios-verification",
    "native-macos": "native-macos-verification",
    "security": "security-review",
    "github": "github-lifecycle",
}


class EvidenceError(ValueError):
    """Evidence could not be read, adapted, validated, or safely written."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise EvidenceError(f"{field} contains unsafe whitespace or control characters")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise EvidenceError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, field: str) -> dt.datetime:
    raw = _text(value, field)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{field} must include a timezone")
    return parsed


def validate_artifact_path(raw_path: Any, project_root: str | Path | None = None) -> str:
    """Return a safe POSIX project-relative artifact reference."""

    path = _text(raw_path, "artifact path")
    if (
        path.startswith(("/", "~"))
        or "\\" in path
        or "\0" in path
        or _WINDOWS_DRIVE_RE.match(path)
        or "://" in path
    ):
        raise EvidenceError(f"artifact path must be project-relative: {path}")
    pure = PurePosixPath(path)
    if str(pure) != path or any(part in {"", ".", ".."} for part in pure.parts):
        raise EvidenceError(f"artifact path is not normalized or escapes the project: {path}")

    if project_root is not None:
        root = Path(project_root).resolve()
        candidate = root.joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise EvidenceError(f"artifact path cannot be resolved: {path}") from exc
        if resolved != root and root not in resolved.parents:
            raise EvidenceError(f"artifact path escapes the project: {path}")
    return path


def _secret_locations(value: Any, path: str = "$") -> list[str]:
    locations: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if (
                _SENSITIVE_KEY_RE.search(str(key))
                and isinstance(child, str)
                and child.strip().casefold() not in _SAFE_PLACEHOLDERS
            ):
                locations.append(child_path)
            locations.extend(_secret_locations(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            locations.extend(_secret_locations(child, f"{path}[{index}]"))
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        locations.append(path)
    return locations


def _validate_artifact(
    artifact: Any,
    index: int,
    *,
    project_root: str | Path | None,
    verify_artifacts: bool,
) -> None:
    if not isinstance(artifact, Mapping):
        raise EvidenceError(f"artifacts[{index}] must be an object")
    path = validate_artifact_path(artifact.get("path"), project_root)
    digest = _sha256(artifact.get("sha256"), f"artifacts[{index}].sha256")
    if "kind" in artifact:
        _text(artifact["kind"], f"artifacts[{index}].kind")
    if "bytes" in artifact and (
        isinstance(artifact["bytes"], bool)
        or not isinstance(artifact["bytes"], int)
        or artifact["bytes"] < 0
    ):
        raise EvidenceError(f"artifacts[{index}].bytes must be a non-negative integer")

    if verify_artifacts:
        if project_root is None:
            raise EvidenceError("project_root is required when verify_artifacts is true")
        file_path = Path(project_root).resolve().joinpath(*PurePosixPath(path).parts)
        if not file_path.is_file():
            raise EvidenceError(f"artifact does not exist as a file: {path}")
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual != digest:
            raise EvidenceError(f"artifact hash does not match file bytes: {path}")
        if "bytes" in artifact and file_path.stat().st_size != artifact["bytes"]:
            raise EvidenceError(f"artifact byte count does not match file bytes: {path}")


def validate_envelope(
    envelope: Any,
    *,
    project_root: str | Path | None = None,
    verify_artifacts: bool = False,
) -> None:
    """Raise EvidenceError unless an envelope satisfies the v2 contract."""

    if not isinstance(envelope, Mapping):
        raise EvidenceError("evidence envelope must be a JSON object")
    missing = [field for field in REQUIRED_FIELDS if field not in envelope]
    if missing:
        raise EvidenceError("evidence envelope is missing fields: " + ", ".join(missing))
    if envelope.get("schema") != EVIDENCE_SCHEMA:
        raise EvidenceError(f"evidence schema must be {EVIDENCE_SCHEMA}")

    for field in ("kind", "task", "capability", "provider"):
        _text(envelope.get(field), field)
    provenance = envelope.get("provenance")
    if not isinstance(provenance, Mapping) or not provenance:
        raise EvidenceError("provenance must be a non-empty object")
    _sha256(envelope.get("source_hash"), "source_hash")
    _sha256(envelope.get("runtime_asset_hash"), "runtime_asset_hash")

    started = _timestamp(envelope.get("started_at"), "started_at")
    finished = _timestamp(envelope.get("finished_at"), "finished_at")
    if finished < started:
        raise EvidenceError("finished_at cannot be earlier than started_at")

    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, list):
        raise EvidenceError("artifacts must be an array")
    paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        _validate_artifact(
            artifact,
            index,
            project_root=project_root,
            verify_artifacts=verify_artifacts,
        )
        path = str(artifact["path"])
        if path in paths:
            raise EvidenceError(f"artifact path appears more than once: {path}")
        paths.add(path)

    if envelope.get("verdict") not in EVIDENCE_VERDICTS:
        raise EvidenceError("verdict must be PASS, FAIL, or DEGRADED")
    blockers = envelope.get("blockers")
    if not isinstance(blockers, list):
        raise EvidenceError("blockers must be an array")
    for index, blocker in enumerate(blockers):
        if isinstance(blocker, str):
            _text(blocker, f"blockers[{index}]")
        elif not isinstance(blocker, Mapping) or not blocker:
            raise EvidenceError(f"blockers[{index}] must be a non-empty string or object")

    secret_locations = _secret_locations(envelope)
    if secret_locations:
        raise EvidenceError(
            "evidence envelope contains secret material at: "
            + ", ".join(sorted(set(secret_locations)))
        )
    try:
        json.dumps(envelope, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("evidence envelope must contain only finite JSON values") from exc


validate_evidence = validate_envelope


def _legacy_blocker(problem: Any) -> dict[str, Any]:
    if isinstance(problem, Mapping):
        blocker = {
            key: problem[key]
            for key in ("severity", "rule", "message", "path", "blocking")
            if key in problem
        }
        return blocker or {"message": "legacy collector reported an unspecified problem"}
    return {"message": str(problem), "blocking": True}


def _legacy_artifacts(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_hashes = payload.get("raw_artifact_hashes")
    raw_hashes = raw_hashes if isinstance(raw_hashes, Mapping) else {}
    source = payload.get("artifacts")
    if isinstance(source, Mapping):
        items = [
            dict(value, kind=str(value.get("kind") or key))
            if isinstance(value, Mapping)
            else {"kind": str(key), "path": value}
            for key, value in source.items()
        ]
    elif isinstance(source, list):
        items = list(source)
    else:
        items = []

    records: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            blockers.append({"message": "legacy manifest contains a malformed artifact", "blocking": True})
            continue
        path = item.get("path")
        raw = raw_hashes.get(path, {}) if isinstance(path, str) else {}
        raw = raw if isinstance(raw, Mapping) else {}
        digest = item.get("sha256") or raw.get("sha256")
        record: dict[str, Any] = {"path": path, "sha256": digest}
        kind = item.get("kind")
        if isinstance(kind, str) and kind.strip():
            record["kind"] = kind
        byte_count = item.get("bytes", raw.get("bytes"))
        if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0:
            record["bytes"] = byte_count
        try:
            safe_path = validate_artifact_path(path)
            _sha256(digest, "legacy artifact sha256")
            if item.get("exists") is False:
                raise EvidenceError(f"legacy artifact does not exist: {safe_path}")
        except EvidenceError as exc:
            if isinstance(path, str):
                rejected.add(path)
            blockers.append({"message": str(exc), "path": str(path or ""), "blocking": True})
            continue
        if safe_path not in seen:
            seen.add(safe_path)
            records.append(record)

    for path, raw in raw_hashes.items():
        if path in seen or path in rejected or not isinstance(raw, Mapping):
            continue
        record = {"path": path, "sha256": raw.get("sha256")}
        if isinstance(raw.get("bytes"), int) and not isinstance(raw.get("bytes"), bool):
            record["bytes"] = raw["bytes"]
        try:
            safe_path = validate_artifact_path(path)
            _sha256(record["sha256"], "legacy artifact sha256")
        except EvidenceError as exc:
            blockers.append({"message": str(exc), "path": str(path), "blocking": True})
            continue
        seen.add(safe_path)
        records.append(record)
    return records, blockers


def adapt_v1_manifest(
    manifest: Mapping[str, Any],
    *,
    capability: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Adapt a legacy live collector manifest to v2 without writing either form."""

    if not isinstance(manifest, Mapping) or manifest.get("schema") != LEGACY_LIVE_MANIFEST_SCHEMA:
        raise EvidenceError(f"legacy manifest schema must be {LEGACY_LIVE_MANIFEST_SCHEMA}")
    collector = _text(manifest.get("collector"), "collector")
    created_at = _text(manifest.get("created_at"), "created_at")
    source_before = manifest.get("source_hash_before")
    source_after = manifest.get("source_hash_after")
    artifacts, artifact_blockers = _legacy_artifacts(manifest)
    problems = manifest.get("problems")
    problem_items = list(problems) if isinstance(problems, list) else []
    blockers = [_legacy_blocker(item) for item in problem_items]
    blockers.extend(artifact_blockers)
    unavailable = manifest.get("unavailable_capabilities")
    if isinstance(unavailable, list):
        blockers.extend(
            {
                "message": f"capability unavailable: {item}",
                "capability": str(item),
                "blocking": False,
            }
            for item in unavailable
            if str(item).strip()
        )
    if source_before != source_after:
        blockers.append(
            {
                "message": "legacy collector changed the source during collection",
                "rule": "source-hash-changed",
                "blocking": True,
            }
        )

    has_blocking = any(
        not isinstance(item, Mapping) or item.get("blocking", True) is not False
        for item in blockers
    )
    degraded = manifest.get("degraded") is True or bool(unavailable) or bool(blockers)
    verdict = "FAIL" if has_blocking else ("DEGRADED" if degraded else "PASS")
    envelope = {
        "schema": EVIDENCE_SCHEMA,
        "kind": collector,
        "task": manifest.get("task"),
        "capability": capability or _CAPABILITY_BY_COLLECTOR.get(collector, collector),
        "provider": provider or collector,
        "provenance": {
            "adapter": LEGACY_LIVE_MANIFEST_SCHEMA,
            "collector": collector,
            "command_argv": manifest.get("command_argv", []),
            "tool_versions": manifest.get("tool_versions", {}),
        },
        "source_hash": source_after,
        "runtime_asset_hash": manifest.get("runtime_asset_hash"),
        "started_at": created_at,
        "finished_at": created_at,
        "artifacts": artifacts,
        "verdict": verdict,
        "blockers": blockers,
    }
    validate_envelope(envelope)
    return envelope


adapt_live_manifest_v1 = adapt_v1_manifest


def read_envelope(
    path: str | Path,
    *,
    allow_v1: bool = True,
    project_root: str | Path | None = None,
    verify_artifacts: bool = False,
) -> dict[str, Any]:
    """Read validated v2 evidence, adapting a v1 live manifest in memory."""

    evidence_path = Path(path)
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read evidence {evidence_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise EvidenceError("evidence file must contain a JSON object")
    if payload.get("schema") == LEGACY_LIVE_MANIFEST_SCHEMA:
        if not allow_v1:
            raise EvidenceError("legacy v1 evidence is not allowed")
        payload = adapt_v1_manifest(payload)
    result = dict(payload)
    validate_envelope(
        result,
        project_root=project_root,
        verify_artifacts=verify_artifacts,
    )
    return result


read_evidence = read_envelope
load_evidence = read_envelope


def write_envelope(
    path: str | Path,
    envelope: Mapping[str, Any] | None = None,
    *,
    project_root: str | Path | None = None,
    verify_artifacts: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    """Validate and atomically write a v2 envelope."""

    if envelope is not None and fields:
        raise EvidenceError("pass either envelope or keyword fields, not both")
    payload: Mapping[str, Any] = envelope if envelope is not None else fields
    validate_envelope(
        payload,
        project_root=project_root,
        verify_artifacts=verify_artifacts,
    )
    serialized = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    evidence_path = Path(path)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{evidence_path.name}.",
            suffix=".tmp",
            dir=evidence_path.parent,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, evidence_path)
    except OSError as exc:
        raise EvidenceError(f"cannot write evidence {evidence_path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
    return json.loads(serialized)


write_evidence = write_envelope
