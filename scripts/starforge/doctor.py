"""Read-only diagnostics for Star Forge and its Codex installation state."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .policy_data import value as _policy_value

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

POLICY = _policy_value("doctor.POLICY")
DOCTOR_SCHEMA = POLICY["schema"]
CANONICAL_MARKETPLACE_URL = POLICY["canonical_marketplace"]
PLUGIN_NAME = POLICY["plugin"]
(
    RULE_STALE_MARKETPLACE,
    RULE_DUPLICATE_INSTALL,
    RULE_ACTIVE_VERSION_DRIFT,
    RULE_STALE_HOOK_TRUST,
    RULE_DUPLICATE_MOBBIN,
) = CHECK_RULES = tuple(POLICY["rules"])

def _finding(rule: str, message: str, paths: Sequence[str], **values: str) -> dict[str, Any]:
    descriptor = POLICY["findings"][rule]
    context = {"plugin": PLUGIN_NAME, "canonical_marketplace": CANONICAL_MARKETPLACE_URL, **values}
    return {
        "rule": rule,
        "severity": descriptor["severity"],
        "message": message,
        "paths": list(paths),
        "remediation": [item.format(**context) for item in descriptor["remediation"]],
    }

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def _read_toml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = tomllib.loads(text)
    except ValueError as exc:
        if tomllib.__name__ != "tomli":
            raise
        line = getattr(exc, "lineno", text.count("\n", 0, getattr(exc, "pos", 0)) + 1)
        raise ValueError(f"unsupported TOML syntax at line {line}") from exc
    return payload if isinstance(payload, dict) else {}

def _path_text(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())

def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None

def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

def _version_core(raw: str) -> str:
    return re.split(r"[+-]", str(raw), maxsplit=1)[0]

def _version_key(raw: str) -> tuple[tuple[int, int | str], ...]:
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"[._-]", _version_core(raw)))

def _first_string(record: Mapping[str, Any], names: Sequence[str]) -> str:
    lowered = {str(key).lower(): value for key, value in record.items()}
    return next((value for name in names if isinstance((value := lowered.get(name)), str) and value), "")

def _manifest_record(manifest_path: Path, codex_home: Path) -> dict[str, Any] | None:
    try:
        payload = _read_json(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("name") != PLUGIN_NAME:
        return None
    root = manifest_path.parent.parent
    try:
        relative = root.relative_to(codex_home / "plugins" / "cache")
        marketplace = relative.parts[0] if relative.parts else ""
    except ValueError:
        marketplace = ""
    return {
        "root": _path_text(root),
        "manifest": _path_text(manifest_path),
        "marketplace": marketplace,
        "version": str(payload.get("version") or root.name),
        "disabled": ".disabled" in marketplace or ".disabled" in root.parts,
        "runtime_sha256": _sha256(root / POLICY["runtime_path"]),
        "hooks_sha256": _sha256(root / POLICY["hooks_path"]),
    }

def _star_forge_installs(codex_home: Path, source_root: Path) -> list[dict[str, Any]]:
    cache_root = codex_home / "plugins" / "cache"
    manifests = set(cache_root.glob(POLICY["manifest_glob"])) if cache_root.is_dir() else set()
    source_manifest = source_root / ".codex-plugin" / "plugin.json"
    if source_manifest.is_file() and _is_relative_to(source_root, codex_home):
        manifests.add(source_manifest)
    records: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for manifest in sorted(manifests, key=str):
        record = _manifest_record(manifest, codex_home)
        if record is not None and record["root"] not in seen_roots:
            records.append(record)
            seen_roots.add(record["root"])
    return records

def _source_record(source_root: Path, runtime_version: str) -> dict[str, Any]:
    manifest_path = source_root / ".codex-plugin" / "plugin.json"
    try:
        payload = _read_json(manifest_path)
        manifest_version = str(payload.get("version") or "") if isinstance(payload, dict) else ""
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest_version = ""
    return {
        "root": _path_text(source_root),
        "manifest": _path_text(manifest_path),
        "version": manifest_version,
        "runtime_version": runtime_version,
        "runtime_sha256": _sha256(source_root / POLICY["runtime_path"]),
        "hooks_sha256": _sha256(source_root / POLICY["hooks_path"]),
    }

def _select_active_install(installs: Sequence[Mapping[str, Any]], source_root: Path, explicit_root: Path | None) -> Mapping[str, Any] | None:
    candidates = [_path_text(explicit_root)] if explicit_root is not None else []
    if plugin_root := os.environ.get("PLUGIN_ROOT", "").strip():
        candidates.append(_path_text(Path(plugin_root).expanduser()))
    for candidate in candidates:
        if match := next((item for item in installs if item.get("root") == candidate), None):
            return match
        if (manifest := Path(candidate) / ".codex-plugin" / "plugin.json").is_file():
            if record := _manifest_record(manifest, source_root.parent):
                return record
    source_text = _path_text(source_root)
    if source_match := next((item for item in installs if item.get("root") == source_text), None):
        return source_match
    pool = [item for item in installs if not item.get("disabled")] or list(installs)
    return sorted(pool, key=lambda item: _version_key(str(item.get("version") or "")))[-1] if pool else None

def _marketplace_mentions(name: str, entry: Mapping[str, Any], installs: Sequence[Mapping[str, Any]]) -> bool:
    if any(str(item.get("marketplace") or "") == name for item in installs):
        return True
    searchable = " ".join([name, *[str(entry.get(field) or "") for field in POLICY["marketplace_search_fields"]]]).lower()
    return PLUGIN_NAME in searchable or PLUGIN_NAME.replace("-", "_") in searchable

def _stale_marketplace_findings(config: Mapping[str, Any], installs: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inspected: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    marketplaces = config.get("marketplaces")
    for name, raw_entry in sorted((marketplaces.items() if isinstance(marketplaces, Mapping) else ()), key=lambda item: str(item[0])):
        if not isinstance(raw_entry, Mapping) or not _marketplace_mentions(str(name), raw_entry, installs):
            continue
        source = _first_string(raw_entry, POLICY["marketplace_source_fields"])
        source_type = _first_string(raw_entry, POLICY["marketplace_type_fields"])
        canonical_source = CANONICAL_MARKETPLACE_URL.lower()
        canonical = source.rstrip("/").lower() in {canonical_source, canonical_source + ".git"} and source_type.lower() not in POLICY["local_marketplace_types"]
        source_exists: bool | None = None
        if source and (source_type.lower() in POLICY["local_marketplace_types"] or source.startswith(("/", "~", "."))):
            source_exists = Path(source).expanduser().exists()
        inspected.append({"name": str(name), "source_type": source_type, "source": source, "source_exists": source_exists, "canonical": canonical})
        if not canonical:
            reason = POLICY["stale_reasons"]["marketplace_missing" if source_exists is False else "marketplace_legacy"]
            findings.append(_finding(RULE_STALE_MARKETPLACE, f"Marketplace {name!r} {reason}.", [source] if source else [], marketplace=str(name)))
    return inspected, findings

def _duplicate_install_findings(installs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(installs) < 2:
        return []
    versions = sorted({str(item.get("version") or "") for item in installs})
    message = f"Found {len(installs)} Star Forge installation roots" + (f" across versions {', '.join(versions)}." if versions else ".")
    return [_finding(RULE_DUPLICATE_INSTALL, message, [str(item.get("root") or "") for item in installs])]

def _active_drift_findings(source: Mapping[str, Any], active: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    reasons: list[str] = []
    source_version = str(source.get("version") or "")
    runtime_version = str(source.get("runtime_version") or "")
    if source_version and runtime_version and _version_core(source_version) != _version_core(runtime_version):
        reasons.append(f"source manifest version {source_version} does not match runtime version {runtime_version}")
    if active is None:
        reasons.append("no active Star Forge cache installation was found")
    else:
        active_version = str(active.get("version") or "")
        if source_version and active_version and source_version != active_version:
            reasons.append(f"active version {active_version} does not match source version {source_version}")
        if source.get("runtime_sha256") and active.get("runtime_sha256") and source["runtime_sha256"] != active["runtime_sha256"]:
            reasons.append("active runtime bytes do not match the source runtime")
    if not reasons:
        return []
    paths = [str(source.get("root") or ""), str(active.get("root") or "") if active is not None else ""]
    return [_finding(RULE_ACTIVE_VERSION_DRIFT, "; ".join(reasons) + ".", [path for path in dict.fromkeys(paths) if path])]

def _hook_trust_paths(codex_home: Path) -> list[Path]:
    candidates = {codex_home / relative for relative in POLICY["hook_trust_paths"]}
    for pattern in POLICY["hook_trust_patterns"]:
        candidates.update(codex_home.glob(pattern))
        if (hooks_dir := codex_home / "hooks").is_dir():
            candidates.update(hooks_dir.glob(pattern))
    return sorted((path for path in candidates if path.is_file()), key=str)

def _walk_star_forge_records(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        encoded = json.dumps(value, sort_keys=True, default=str).lower()
        if PLUGIN_NAME in encoded or PLUGIN_NAME.replace("-", "_") in encoded or PLUGIN_NAME in ".".join(path).lower():
            yield path, value
        for key, child in value.items():
            yield from _walk_star_forge_records(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_star_forge_records(child, path + (str(index),))

def _hook_stale_reasons(record: Mapping[str, Any], source: Mapping[str, Any], install_roots: set[str]) -> list[str]:
    reasons: list[str] = []
    version = _first_string(record, POLICY["hook_version_fields"])
    source_version = str(source.get("version") or "")
    if version and source_version and version != source_version:
        reasons.append(f"trusted version {version} does not match source version {source_version}")
    if root_value := _first_string(record, POLICY["hook_root_fields"]):
        root = Path(root_value).expanduser()
        if not root.exists():
            reasons.append(POLICY["stale_reasons"]["hook_missing_root"])
        elif install_roots and _path_text(root) not in install_roots and _path_text(root) != str(source.get("root") or ""):
            reasons.append(POLICY["stale_reasons"]["hook_inactive_root"])
    hook_path = _first_string(record, POLICY["hook_path_fields"])
    trusted_hash = _first_string(record, POLICY["hook_hash_fields"])
    if hook_path and trusted_hash:
        if (current_hash := _sha256(Path(hook_path).expanduser())) and current_hash != trusted_hash:
            reasons.append(POLICY["stale_reasons"]["hook_file_hash"])
    elif trusted_hash and source.get("hooks_sha256") and trusted_hash != source.get("hooks_sha256"):
        reasons.append(POLICY["stale_reasons"]["hook_manifest_hash"])
    return reasons

def _stale_hook_findings(codex_home: Path, source: Mapping[str, Any], installs: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    roots = {str(item.get("root") or "") for item in installs}
    seen: set[tuple[str, str]] = set()
    for path in _hook_trust_paths(codex_home):
        try:
            payload = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for record_path, record in _walk_star_forge_records(payload):
            if not (reasons := _hook_stale_reasons(record, source, roots)):
                continue
            locator = ".".join(record_path) or "<root>"
            key = (_path_text(path), locator)
            if key in seen:
                continue
            seen.add(key)
            records.append({"file": key[0], "record": locator, "reasons": reasons})
            findings.append(_finding(RULE_STALE_HOOK_TRUST, f"Stale Star Forge hook trust record at {locator}: {'; '.join(reasons)}.", [key[0]]))
    return records, findings

def _mobbin_toml_connections(value: Any, path: tuple[str, ...] = ()) -> Iterable[str]:
    if not isinstance(value, Mapping):
        return
    context = any(token in part.lower() for part in path for token in POLICY["mobbin_context_tokens"])
    for key, child in value.items():
        child_path = path + (str(key),)
        if "mobbin" in str(key).lower() and (context or isinstance(child, Mapping)):
            yield ".".join(child_path)
        elif isinstance(child, Mapping):
            yield from _mobbin_toml_connections(child, child_path)

def _connection_json_paths(codex_home: Path) -> list[Path]:
    candidates: set[Path] = set()
    root = codex_home / "plugins" / "cache"
    if root.is_dir():
        for pattern in POLICY["connection_globs"]:
            candidates.update(root.rglob(pattern))
    candidates.update(codex_home / name for name in POLICY["connection_json_names"] if (codex_home / name).is_file())
    return sorted(candidates, key=str)

def _mobbin_connections(codex_home: Path, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    config_path = codex_home / "config.toml"
    connections = [{"kind": "config", "locator": f"{_path_text(config_path)}#{table}"} for table in dict.fromkeys(_mobbin_toml_connections(config))]
    for path in _connection_json_paths(codex_home):
        try:
            payload = _read_json(path)
            contains_mobbin = "mobbin" in json.dumps(payload, sort_keys=True, default=str).lower()
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        except (TypeError, ValueError):
            contains_mobbin = "mobbin" in str(payload).lower()
        if contains_mobbin:
            connections.append({"kind": "app" if path.name == ".app.json" else "mcp", "locator": _path_text(path)})
    unique = {str(item["locator"]): item for item in connections}
    return [unique[key] for key in sorted(unique)]

def _duplicate_mobbin_findings(connections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(connections) < 2:
        return []
    return [_finding(RULE_DUPLICATE_MOBBIN, f"Found {len(connections)} Mobbin connection definitions.", [str(item.get("locator") or "") for item in connections])]

def _check(rule: str, findings: Sequence[Mapping[str, Any]], details: Any) -> dict[str, Any]:
    relevant = [dict(item) for item in findings if item.get("rule") == rule]
    return {"id": rule, "status": "warning" if relevant else "pass", "finding_count": len(relevant), "details": details}

def diagnose_installation(
    *,
    codex_home: Path,
    source_root: Path,
    runtime_version: str,
    active_plugin_root: Path | None = None,
) -> dict[str, Any]:
    """Inspect installation state without creating, changing, or deleting files."""
    codex_home = codex_home.expanduser()
    source_root = source_root.expanduser()
    config_path = codex_home / "config.toml"
    config: dict[str, Any] = {}
    config_error: str | None = None
    if config_path.is_file():
        try:
            config = _read_toml(config_path)
        except (OSError, UnicodeError, ValueError) as exc:
            config_error = f"{type(exc).__name__}: {exc}"
    installs = _star_forge_installs(codex_home, source_root)
    source = _source_record(source_root, runtime_version)
    active = _select_active_install(installs, source_root, active_plugin_root)
    marketplaces, marketplace_findings = _stale_marketplace_findings(config, installs)
    hook_records, hook_findings = _stale_hook_findings(codex_home, source, installs)
    connections = _mobbin_connections(codex_home, config)
    findings = marketplace_findings + _duplicate_install_findings(installs) + _active_drift_findings(source, active) + hook_findings + _duplicate_mobbin_findings(connections)
    details = {
        RULE_STALE_MARKETPLACE: marketplaces,
        RULE_DUPLICATE_INSTALL: installs,
        RULE_ACTIVE_VERSION_DRIFT: {"source": source, "active": dict(active) if active is not None else None},
        RULE_STALE_HOOK_TRUST: hook_records,
        RULE_DUPLICATE_MOBBIN: connections,
    }
    return {
        "schema": DOCTOR_SCHEMA,
        "verdict": "ATTENTION" if findings else "PASS",
        "read_only": True,
        "codex_home": _path_text(codex_home),
        "source_root": _path_text(source_root),
        "config": {"path": _path_text(config_path), "readable": config_error is None, "error": config_error},
        "checks": [_check(rule, findings, details[rule]) for rule in CHECK_RULES],
        "finding_count": len(findings),
        "findings": findings,
    }

def doctor_exit_code(payload: Mapping[str, Any], *, strict: bool) -> int:
    return int(strict and payload.get("verdict") != "PASS")
