"""Versioned, source-bound evidence envelopes for Star Forge."""

from __future__ import annotations
from .policy_data import mapping as _policy_mapping, value as _policy_value

import datetime as dt
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from . import safe_io

EVIDENCE_POLICY = _policy_value("runtime_evidence.POLICY")
globals().update(EVIDENCE_POLICY["bindings"])
_PATTERNS = {name: re.compile(pattern, re.IGNORECASE if name in {"secret_value", "sensitive_key"} else 0) for name, pattern in EVIDENCE_POLICY["patterns"].items()}
_SHA256_RE, _WINDOWS_DRIVE_RE, _SECRET_VALUE_RE, _SENSITIVE_KEY_RE = map(_PATTERNS.__getitem__, ("sha256", "windows_drive", "secret_value", "sensitive_key"))

class EvidenceError(ValueError):
    pass

def _error(name: str, **values: object) -> EvidenceError:
    return EvidenceError(EVIDENCE_POLICY["errors"][name].format(**values))

def _require(condition: object, name: str, **values: object) -> None:
    if not condition:
        raise _error(name, **values)

def _text(value: Any, field: str) -> str:
    _require(isinstance(value, str) and value.strip(), "text_required", field=field)
    _require(value == value.strip() and all(ord(char) >= 32 for char in value),
             "text_unsafe", field=field)
    return value

def _sha256(value: Any, field: str) -> str:
    _require(isinstance(value, str) and _SHA256_RE.fullmatch(value), "sha256", field=field)
    return value

def _timestamp(value: Any, field: str) -> dt.datetime:
    raw = _text(value, field)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _error("timestamp", field=field) from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "timezone", field=field)
    return parsed

def validate_artifact_path(raw_path: Any, project_root: str | Path | None = None) -> str:
    path = _text(raw_path, "artifact path")
    _require(not (path.startswith(("/", "~")) or "\\" in path or "\0" in path
                  or _WINDOWS_DRIVE_RE.match(path) or "://" in path), "path_relative", path=path)
    pure = PurePosixPath(path)
    _require(str(pure) == path and all(part not in {"", ".", ".."} for part in pure.parts),
             "path_normalized", path=path)
    if project_root is not None:
        root = Path(project_root).resolve()
        candidate = root.joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise _error("path_resolve", path=path) from exc
        _require(resolved == root or root in resolved.parents, "path_escape", path=path)
    return path

def _secret_locations(value: Any, path: str = "$") -> list[str]:
    if isinstance(value, Mapping):
        return [
            location for key, child in value.items()
            for child_path in (f"{path}.{key}",)
            for location in (
                ([child_path] if _SENSITIVE_KEY_RE.search(str(key)) and isinstance(child, str)
                  and child.strip().casefold() not in _SAFE_PLACEHOLDERS else [])
                + _secret_locations(child, child_path))]
    if isinstance(value, list):
        return [location for index, child in enumerate(value)
                for location in _secret_locations(child, f"{path}[{index}]")]
    return [path] if isinstance(value, str) and _SECRET_VALUE_RE.search(value) else []

def _validate_artifact(artifact: Any, index: int, *, project_root: str | Path | None,
                       verify_artifacts: bool) -> None:
    _require(isinstance(artifact, Mapping), "artifact_object", index=index)
    path = validate_artifact_path(artifact.get("path"), project_root)
    digest = _sha256(artifact.get("sha256"), f"artifacts[{index}].sha256")
    if "kind" in artifact:
        _text(artifact["kind"], f"artifacts[{index}].kind")
    byte_count = artifact.get("bytes")
    _require("bytes" not in artifact or isinstance(byte_count, int)
             and not isinstance(byte_count, bool) and byte_count >= 0, "artifact_bytes", index=index)
    if verify_artifacts:
        _require(project_root is not None, "project_root")
        file_path = Path(project_root).resolve().joinpath(*PurePosixPath(path).parts)
        try:
            actual, actual_size = safe_io.digest_size(project_root, file_path)
        except OSError as exc:
            raise _error("artifact_missing", path=path) from exc
        _require(actual == digest, "artifact_hash", path=path)
        _require("bytes" not in artifact or actual_size == byte_count,
                 "artifact_size", path=path)

def validate_envelope(envelope: Any, *, project_root: str | Path | None = None,
                      verify_artifacts: bool = False) -> None:
    _require(isinstance(envelope, Mapping), "envelope_object")
    missing = [field for field in REQUIRED_FIELDS if field not in envelope]
    _require(not missing, "envelope_missing", fields=", ".join(missing))
    _require(envelope.get("schema") == EVIDENCE_SCHEMA, "schema", schema=EVIDENCE_SCHEMA)
    for field in EVIDENCE_POLICY["identity_fields"]:
        _text(envelope.get(field), field)
    provenance = envelope.get("provenance")
    _require(isinstance(provenance, Mapping) and provenance, "provenance")
    for field in EVIDENCE_POLICY["hash_fields"]:
        _sha256(envelope.get(field), field)
    times = [_timestamp(envelope.get(field), field)
             for field in EVIDENCE_POLICY["timestamp_fields"]]
    _require(times[1] >= times[0], "time_order")
    artifacts = envelope.get("artifacts")
    _require(isinstance(artifacts, list), "artifacts_array")
    for index, artifact in enumerate(artifacts):
        _validate_artifact(artifact, index, project_root=project_root,
                           verify_artifacts=verify_artifacts)
    paths = [str(artifact["path"]) for artifact in artifacts]
    duplicate = next(
        (path for index, path in enumerate(paths) if path in paths[:index]), None)
    _require(duplicate is None, "artifact_duplicate", path=duplicate)
    _require(envelope.get("verdict") in EVIDENCE_VERDICTS, "verdict")
    blockers = envelope.get("blockers")
    _require(isinstance(blockers, list), "blockers_array")
    for index, blocker in enumerate(blockers):
        (_text(blocker, f"blockers[{index}]") if isinstance(blocker, str)
         else _require(isinstance(blocker, Mapping) and blocker, "blocker", index=index))
    secret_locations = _secret_locations(envelope)
    _require(not secret_locations, "secret",
             locations=", ".join(sorted(set(secret_locations))))
    try:
        json.dumps(envelope, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _error("json_finite") from exc

def _legacy_artifacts(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_hashes = payload.get("raw_artifact_hashes")
    raw_hashes = raw_hashes if isinstance(raw_hashes, Mapping) else {}
    source = payload.get("artifacts")
    named = isinstance(source, Mapping)
    entries = source.items() if named else enumerate(source) if isinstance(source, list) else ()
    items = [
        (dict(value, kind=str(value.get("kind") or key)) if isinstance(value, Mapping)
         else {"kind": str(key), "path": value}) if named else value
        for key, value in entries]
    records: dict[str, dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    rejected: set[str] = set()

    def append(item: Any, raw: Any, from_manifest: bool) -> None:
        if not isinstance(item, Mapping):
            blockers.append(dict(EVIDENCE_POLICY["legacy_malformed_artifact"]))
            return
        path = item.get("path")
        raw = raw if isinstance(raw, Mapping) else {}
        kind, byte_count = item.get("kind"), item.get("bytes", raw.get("bytes"))
        record = _policy_mapping(
            "legacy_artifact", path=path, sha256=item.get("sha256") or raw.get("sha256"),
            kind=kind if isinstance(kind, str) and kind.strip() else None,
            bytes=byte_count if isinstance(byte_count, int) and not isinstance(byte_count, bool)
            and (byte_count >= 0 or not from_manifest) else None)
        record = {key: value for key, value in record.items() if value is not None}
        try:
            safe_path = validate_artifact_path(path)
            _sha256(record.get("sha256"), "legacy artifact sha256")
            if from_manifest and item.get("exists") is False:
                raise _error("legacy_artifact_missing", path=safe_path)
        except EvidenceError as exc:
            if from_manifest and isinstance(path, str):
                rejected.add(path)
            path_text = str(path or "") if from_manifest else str(path)
            blockers.append({"message": str(exc), "path": path_text, "blocking": True})
        else:
            records.setdefault(safe_path, record)

    for item in items:
        path = item.get("path") if isinstance(item, Mapping) else None
        append(item, raw_hashes.get(path, {}) if isinstance(path, str) else {}, True)
    for path, raw in raw_hashes.items():
        if path not in records and path not in rejected and isinstance(raw, Mapping):
            append({"path": path}, raw, False)
    return list(records.values()), blockers

def adapt_v1_manifest(manifest: Mapping[str, Any], *, capability: str | None = None,
                      provider: str | None = None) -> dict[str, Any]:
    _require(isinstance(manifest, Mapping)
             and manifest.get("schema") == LEGACY_LIVE_MANIFEST_SCHEMA,
             "legacy_schema", schema=LEGACY_LIVE_MANIFEST_SCHEMA)
    collector = _text(manifest.get("collector"), "collector")
    created_at = _text(manifest.get("created_at"), "created_at")
    source_after = manifest.get("source_hash_after")
    artifacts, artifact_blockers = _legacy_artifacts(manifest)
    problems, unavailable = manifest.get("problems"), manifest.get("unavailable_capabilities")
    problems = problems if isinstance(problems, list) else []
    unavailable = unavailable if isinstance(unavailable, list) else []
    policy = EVIDENCE_POLICY["legacy_unavailable"]
    blockers = [
        ({key: item[key] for key in EVIDENCE_POLICY["legacy_blocker_fields"] if key in item}
         or dict(EVIDENCE_POLICY["legacy_problem_defaults"]))
        if isinstance(item, Mapping) else {"message": str(item), "blocking": True}
        for item in problems] + artifact_blockers + [
        _policy_mapping(
            "legacy_unavailable_blocker", message=policy["message"].format(capability=item),
            capability=str(item), blocking=policy["blocking"])
        for item in unavailable if str(item).strip()]
    if manifest.get("source_hash_before") != source_after:
        blockers.append(dict(EVIDENCE_POLICY["legacy_source_changed"]))
    has_blocking = any(not isinstance(item, Mapping) or item.get("blocking", True) is not False for item in blockers)
    degraded = manifest.get("degraded") is True or bool(unavailable) or bool(blockers)
    verdicts = EVIDENCE_POLICY["verdict_by_state"]
    verdict = verdicts["blocking"] if has_blocking else verdicts["degraded"] if degraded else verdicts["passing"]
    provenance = _policy_mapping(
        "legacy_provenance", adapter=LEGACY_LIVE_MANIFEST_SCHEMA, collector=collector,
        command_argv=manifest.get("command_argv", []),
        tool_versions=manifest.get("tool_versions", {}))
    envelope = _policy_mapping(
        "evidence_envelope", schema=EVIDENCE_SCHEMA, kind=collector,
        task=manifest.get("task"),
        capability=capability or _CAPABILITY_BY_COLLECTOR.get(collector, collector),
        provider=provider or collector, provenance=provenance, source_hash=source_after,
        runtime_asset_hash=manifest.get("runtime_asset_hash"), started_at=created_at,
        finished_at=created_at, artifacts=artifacts, verdict=verdict, blockers=blockers)
    validate_envelope(envelope)
    return envelope

def read_envelope(path: str | Path, *, allow_v1: bool = True,
                  project_root: str | Path | None = None,
                  verify_artifacts: bool = False) -> dict[str, Any]:
    evidence_path = Path(path)
    root = Path(project_root) if project_root is not None else safe_io.infer_root(evidence_path)
    try:
        payload = json.loads(safe_io.read_text(root, evidence_path))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error("read", path=evidence_path, error=exc) from exc
    _require(isinstance(payload, Mapping), "file_object")
    if payload.get("schema") == LEGACY_LIVE_MANIFEST_SCHEMA:
        _require(allow_v1, "legacy_disallowed")
        payload = adapt_v1_manifest(payload)
    result = dict(payload)
    validate_envelope(result, project_root=project_root, verify_artifacts=verify_artifacts)
    return result

def write_envelope(path: str | Path, envelope: Mapping[str, Any] | None = None, *,
                   project_root: str | Path | None = None,
                   verify_artifacts: bool = False, **fields: Any) -> dict[str, Any]:
    _require(envelope is None or not fields, "write_conflict")
    payload: Mapping[str, Any] = envelope if envelope is not None else fields
    validate_envelope(payload, project_root=project_root, verify_artifacts=verify_artifacts)
    serialized = json.dumps(payload, **EVIDENCE_POLICY["json_format"]) + "\n"
    evidence_path = Path(path)
    try:
        safe_io.atomic_write_text(
            Path(project_root) if project_root is not None
            else safe_io.infer_root(evidence_path),
            evidence_path,
            serialized,
        )
    except OSError as exc:
        raise _error("write", path=evidence_path, error=exc) from exc
    return json.loads(serialized)

globals().update({
    alias: globals()[target]
    for alias, target in EVIDENCE_POLICY["aliases"].items()
})
for name, text in EVIDENCE_POLICY["docs"].items():
    globals()[name].__doc__ = text
