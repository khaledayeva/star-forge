"""Read-only diagnostics for Star Forge and its Codex installation state."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    tomllib = None  # type: ignore[assignment]


DOCTOR_SCHEMA = "star-forge.doctor.v1"
CANONICAL_MARKETPLACE_URL = "https://github.com/khaledayeva/star-forge"
PLUGIN_NAME = "star-forge"

RULE_STALE_MARKETPLACE = "stale-marketplace"
RULE_DUPLICATE_INSTALL = "duplicate-star-forge-install"
RULE_ACTIVE_VERSION_DRIFT = "active-version-drift"
RULE_STALE_HOOK_TRUST = "stale-hook-trust"
RULE_DUPLICATE_MOBBIN = "duplicate-mobbin-connection"

CHECK_RULES = (
    RULE_STALE_MARKETPLACE,
    RULE_DUPLICATE_INSTALL,
    RULE_ACTIVE_VERSION_DRIFT,
    RULE_STALE_HOOK_TRUST,
    RULE_DUPLICATE_MOBBIN,
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_toml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        payload = tomllib.loads(text)
    else:
        payload = _read_toml_compat(text)
    return payload if isinstance(payload, dict) else {}


def _toml_path(raw: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in raw.strip():
        if escaped:
            current.append(char)
            escaped = False
        elif quote and char == "\\" and quote == '"':
            escaped = True
        elif char in {'"', "'"}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            else:
                current.append(char)
        elif char == "." and not quote:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote:
        raise ValueError("unterminated quoted TOML key")
    parts.append("".join(current).strip())
    if not all(parts):
        raise ValueError("empty TOML key")
    return parts


def _toml_value(raw: str) -> Any:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _strip_toml_comment(raw: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
        elif quote and char == "\\" and quote == '"':
            escaped = True
        elif char in {'"', "'"}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
        elif char == "#" and not quote:
            return raw[:index]
    return raw


def _read_toml_compat(text: str) -> dict[str, Any]:
    """Read the small config.toml subset needed by the doctor on Python 3.9."""
    payload: dict[str, Any] = {}
    current: dict[str, Any] = payload
    for line_number, original in enumerate(text.splitlines(), start=1):
        line = _strip_toml_comment(original).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]") and not line.startswith("[["):
            current = payload
            for part in _toml_path(line[1:-1]):
                child = current.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ValueError(f"TOML table collision at line {line_number}")
                current = child
            continue
        if "=" not in line:
            raise ValueError(f"unsupported TOML syntax at line {line_number}")
        raw_key, raw_value = line.split("=", 1)
        target = current
        key_parts = _toml_path(raw_key)
        for part in key_parts[:-1]:
            child = target.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"TOML key collision at line {line_number}")
            target = child
        target[key_parts[-1]] = _toml_value(raw_value)
    return payload


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _path_text(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _version_core(raw: str) -> str:
    return re.split(r"[+-]", str(raw), maxsplit=1)[0]


def _version_key(raw: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for piece in re.split(r"[._-]", _version_core(raw)):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece.lower()))
    return tuple(parts)


def _manifest_record(manifest_path: Path, codex_home: Path) -> dict[str, Any] | None:
    try:
        payload = _read_json(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("name") != PLUGIN_NAME:
        return None
    root = manifest_path.parent.parent
    cache_root = codex_home / "plugins" / "cache"
    marketplace = ""
    try:
        relative = root.relative_to(cache_root)
        marketplace = relative.parts[0] if relative.parts else ""
    except ValueError:
        pass
    return {
        "root": _path_text(root),
        "manifest": _path_text(manifest_path),
        "marketplace": marketplace,
        "version": str(payload.get("version") or root.name),
        "disabled": ".disabled" in marketplace or ".disabled" in root.parts,
        "runtime_sha256": _sha256(root / "scripts" / "star_forge.py"),
        "hooks_sha256": _sha256(root / "hooks" / "hooks.json"),
    }


def _star_forge_installs(codex_home: Path, source_root: Path) -> list[dict[str, Any]]:
    manifests: set[Path] = set()
    cache_root = codex_home / "plugins" / "cache"
    if cache_root.is_dir():
        manifests.update(cache_root.glob("*/star-forge/*/.codex-plugin/plugin.json"))
    source_manifest = source_root / ".codex-plugin" / "plugin.json"
    if source_manifest.is_file() and _is_relative_to(source_root, codex_home):
        manifests.add(source_manifest)

    records: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for manifest in sorted(manifests, key=lambda item: str(item)):
        record = _manifest_record(manifest, codex_home)
        if record is None or record["root"] in seen_roots:
            continue
        records.append(record)
        seen_roots.add(record["root"])
    return records


def _source_record(source_root: Path, runtime_version: str) -> dict[str, Any]:
    manifest_path = source_root / ".codex-plugin" / "plugin.json"
    manifest_version = ""
    try:
        payload = _read_json(manifest_path)
        if isinstance(payload, dict):
            manifest_version = str(payload.get("version") or "")
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {
        "root": _path_text(source_root),
        "manifest": _path_text(manifest_path),
        "version": manifest_version,
        "runtime_version": runtime_version,
        "runtime_sha256": _sha256(source_root / "scripts" / "star_forge.py"),
        "hooks_sha256": _sha256(source_root / "hooks" / "hooks.json"),
    }


def _marketplaces(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("marketplaces")
    return value if isinstance(value, Mapping) else {}


def _marketplace_mentions_star_forge(
    name: str,
    entry: Mapping[str, Any],
    installs: Sequence[Mapping[str, Any]],
) -> bool:
    if any(str(item.get("marketplace") or "") == name for item in installs):
        return True
    searchable = " ".join(
        [
            name,
            str(entry.get("source") or ""),
            str(entry.get("path") or ""),
            str(entry.get("url") or ""),
        ]
    ).lower()
    return "star-forge" in searchable or "star_forge" in searchable


def _is_canonical_marketplace(entry: Mapping[str, Any]) -> bool:
    source = str(entry.get("source") or entry.get("url") or "").rstrip("/").lower()
    source_type = str(entry.get("source_type") or entry.get("type") or "").lower()
    canonical = CANONICAL_MARKETPLACE_URL.lower()
    return source in {canonical, canonical + ".git"} and source_type not in {"local", "path"}


def _stale_marketplace_findings(
    config: Mapping[str, Any],
    installs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inspected: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for name, raw_entry in sorted(_marketplaces(config).items(), key=lambda item: str(item[0])):
        if not isinstance(raw_entry, Mapping):
            continue
        entry = dict(raw_entry)
        if not _marketplace_mentions_star_forge(str(name), entry, installs):
            continue
        source = str(entry.get("source") or entry.get("url") or entry.get("path") or "")
        canonical = _is_canonical_marketplace(entry)
        source_exists: bool | None = None
        source_type = str(entry.get("source_type") or entry.get("type") or "")
        if source and (source_type.lower() in {"local", "path"} or source.startswith(("/", "~", "."))):
            source_exists = Path(source).expanduser().exists()
        inspected.append(
            {
                "name": str(name),
                "source_type": source_type,
                "source": source,
                "source_exists": source_exists,
                "canonical": canonical,
            }
        )
        if canonical:
            continue
        reason = "uses a legacy local marketplace source"
        if source_exists is False:
            reason = "points to a marketplace source that no longer exists"
        findings.append(
            {
                "rule": RULE_STALE_MARKETPLACE,
                "severity": "warning",
                "message": f"Marketplace {name!r} {reason}.",
                "paths": [source] if source else [],
                "remediation": [
                    f"codex plugin remove {PLUGIN_NAME}@{name}",
                    f"codex plugin marketplace remove {name}",
                    f"codex plugin marketplace add {CANONICAL_MARKETPLACE_URL}",
                    f"codex plugin add {PLUGIN_NAME}@{PLUGIN_NAME}",
                ],
            }
        )
    return inspected, findings


def _duplicate_install_findings(
    installs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(installs) < 2:
        return []
    roots = [str(item.get("root") or "") for item in installs]
    versions = sorted({str(item.get("version") or "") for item in installs})
    return [
        {
            "rule": RULE_DUPLICATE_INSTALL,
            "severity": "warning",
            "message": (
                f"Found {len(installs)} Star Forge installation roots"
                + (f" across versions {', '.join(versions)}." if versions else ".")
            ),
            "paths": roots,
            "remediation": [
                "Keep only the canonical GitHub-backed Star Forge marketplace registration.",
                "Review the listed roots, then remove obsolete installs with the Codex plugin commands shown by the marketplace diagnostic.",
            ],
        }
    ]


def _select_active_install(
    installs: Sequence[Mapping[str, Any]],
    source_root: Path,
    explicit_active_root: Path | None,
) -> Mapping[str, Any] | None:
    candidates: list[str] = []
    if explicit_active_root is not None:
        candidates.append(_path_text(explicit_active_root))
    plugin_root_env = os.environ.get("PLUGIN_ROOT", "").strip()
    if plugin_root_env:
        candidates.append(_path_text(Path(plugin_root_env).expanduser()))
    source_text = _path_text(source_root)
    for candidate in candidates:
        match = next((item for item in installs if item.get("root") == candidate), None)
        if match is not None:
            return match
        manifest = Path(candidate) / ".codex-plugin" / "plugin.json"
        if manifest.is_file():
            record = _manifest_record(manifest, source_root.parent)
            if record is not None:
                return record
    source_match = next(
        (item for item in installs if item.get("root") == source_text),
        None,
    )
    if source_match is not None:
        return source_match
    enabled = [item for item in installs if not item.get("disabled")]
    pool = enabled or list(installs)
    if not pool:
        return None
    return sorted(pool, key=lambda item: _version_key(str(item.get("version") or "")))[-1]


def _active_drift_findings(
    source: Mapping[str, Any],
    active: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    reasons: list[str] = []
    source_version = str(source.get("version") or "")
    runtime_version = str(source.get("runtime_version") or "")
    if source_version and runtime_version and _version_core(source_version) != _version_core(runtime_version):
        reasons.append(
            f"source manifest version {source_version} does not match runtime version {runtime_version}"
        )
    if active is None:
        reasons.append("no active Star Forge cache installation was found")
    else:
        active_version = str(active.get("version") or "")
        if source_version and active_version and source_version != active_version:
            reasons.append(
                f"active version {active_version} does not match source version {source_version}"
            )
        source_runtime = source.get("runtime_sha256")
        active_runtime = active.get("runtime_sha256")
        if source_runtime and active_runtime and source_runtime != active_runtime:
            reasons.append("active runtime bytes do not match the source runtime")
    if not reasons:
        return []
    paths = [str(source.get("root") or "")]
    if active is not None:
        paths.append(str(active.get("root") or ""))
    return [
        {
            "rule": RULE_ACTIVE_VERSION_DRIFT,
            "severity": "warning",
            "message": "; ".join(reasons) + ".",
            "paths": [path for path in dict.fromkeys(paths) if path],
            "remediation": [
                f"Reinstall {PLUGIN_NAME} from {CANONICAL_MARKETPLACE_URL}, then start a new Codex task.",
            ],
        }
    ]


def _hook_trust_paths(codex_home: Path) -> list[Path]:
    candidates = {
        codex_home / ".codex-global-state.json",
        codex_home / "hook-trust.json",
        codex_home / "hooks-trust.json",
        codex_home / "trusted-hooks.json",
        codex_home / "hooks" / "trust.json",
        codex_home / "hooks" / "trusted.json",
        codex_home / "plugins" / "hook-trust.json",
    }
    for pattern in ("*hook*trust*.json", "*trust*hook*.json"):
        candidates.update(codex_home.glob(pattern))
        hooks_dir = codex_home / "hooks"
        if hooks_dir.is_dir():
            candidates.update(hooks_dir.glob(pattern))
    return sorted((path for path in candidates if path.is_file()), key=lambda item: str(item))


def _walk_star_forge_records(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> Iterable[tuple[tuple[str, ...], Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        encoded = json.dumps(value, sort_keys=True, default=str).lower()
        path_text = ".".join(path).lower()
        if "star-forge" in encoded or "star_forge" in encoded or "star-forge" in path_text:
            yield path, value
        for key, child in value.items():
            yield from _walk_star_forge_records(child, path=path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_star_forge_records(child, path=path + (str(index),))


def _first_string(record: Mapping[str, Any], names: Sequence[str]) -> str:
    lowered = {str(key).lower(): value for key, value in record.items()}
    for name in names:
        value = lowered.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _hook_record_stale_reasons(
    record: Mapping[str, Any],
    source: Mapping[str, Any],
    install_roots: set[str],
) -> list[str]:
    reasons: list[str] = []
    source_version = str(source.get("version") or "")
    version = _first_string(record, ("version", "plugin_version", "trusted_version"))
    if version and source_version and version != source_version:
        reasons.append(f"trusted version {version} does not match source version {source_version}")

    root_value = _first_string(
        record,
        ("plugin_root", "root", "path", "plugin_path", "source_path"),
    )
    if root_value:
        root_path = Path(root_value).expanduser()
        root_text = _path_text(root_path)
        if not root_path.exists():
            reasons.append("trusted plugin path no longer exists")
        elif install_roots and root_text not in install_roots and root_text != str(source.get("root") or ""):
            reasons.append("trusted plugin path is not an active installation root")

    hook_path_value = _first_string(record, ("hooks_path", "hook_path", "manifest_path"))
    trusted_hash = _first_string(record, ("hooks_sha256", "hook_sha256", "sha256", "hash"))
    if hook_path_value and trusted_hash:
        current_hash = _sha256(Path(hook_path_value).expanduser())
        if current_hash and current_hash != trusted_hash:
            reasons.append("trusted hook hash does not match the current hook file")
    elif trusted_hash and source.get("hooks_sha256") and trusted_hash != source.get("hooks_sha256"):
        reasons.append("trusted hook hash does not match the source hook manifest")
    return reasons


def _stale_hook_findings(
    codex_home: Path,
    source: Mapping[str, Any],
    installs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    install_roots = {str(item.get("root") or "") for item in installs}
    seen: set[tuple[str, str]] = set()
    for path in _hook_trust_paths(codex_home):
        try:
            payload = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for record_path, record in _walk_star_forge_records(payload):
            reasons = _hook_record_stale_reasons(record, source, install_roots)
            if not reasons:
                continue
            locator = ".".join(record_path) or "<root>"
            key = (_path_text(path), locator)
            if key in seen:
                continue
            seen.add(key)
            details = {
                "file": _path_text(path),
                "record": locator,
                "reasons": reasons,
            }
            records.append(details)
            findings.append(
                {
                    "rule": RULE_STALE_HOOK_TRUST,
                    "severity": "warning",
                    "message": f"Stale Star Forge hook trust record at {locator}: {'; '.join(reasons)}.",
                    "paths": [_path_text(path)],
                    "remediation": [
                        "Open /hooks in a new Codex task and review the current Star Forge hook entries.",
                        "Remove obsolete trust entries only after confirming their listed source paths are no longer needed.",
                    ],
                }
            )
    return records, findings


def _contains_mobbin(value: Any) -> bool:
    try:
        return "mobbin" in json.dumps(value, sort_keys=True, default=str).lower()
    except (TypeError, ValueError):
        return "mobbin" in str(value).lower()


def _mobbin_toml_connections(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> Iterable[str]:
    if not isinstance(value, Mapping):
        return
    connection_context = any(
        token in part.lower()
        for part in path
        for token in ("mcp", "app", "connector", "connection", "plugin")
    )
    for key, child in value.items():
        child_path = path + (str(key),)
        if "mobbin" in str(key).lower() and (connection_context or isinstance(child, Mapping)):
            yield ".".join(child_path)
            continue
        if isinstance(child, Mapping):
            yield from _mobbin_toml_connections(child, path=child_path)


def _connection_json_paths(codex_home: Path, source_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for root in (codex_home / "plugins" / "cache", source_root):
        if not root.is_dir():
            continue
        candidates.update(root.rglob(".app.json"))
        candidates.update(root.rglob(".mcp.json"))
    for name in ("connections.json", "apps.json", "mcp.json"):
        path = codex_home / name
        if path.is_file():
            candidates.add(path)
    return sorted(candidates, key=lambda item: str(item))


def _mobbin_connections(
    codex_home: Path,
    source_root: Path,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    connections: list[dict[str, Any]] = []
    config_path = codex_home / "config.toml"
    for table in dict.fromkeys(_mobbin_toml_connections(config)):
        connections.append(
            {
                "kind": "config",
                "locator": f"{_path_text(config_path)}#{table}",
            }
        )
    for path in _connection_json_paths(codex_home, source_root):
        try:
            payload = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if _contains_mobbin(payload):
            connections.append(
                {
                    "kind": "app" if path.name == ".app.json" else "mcp",
                    "locator": _path_text(path),
                }
            )
    unique: dict[str, dict[str, Any]] = {}
    for connection in connections:
        unique[str(connection["locator"])] = connection
    return [unique[key] for key in sorted(unique)]


def _duplicate_mobbin_findings(
    connections: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(connections) < 2:
        return []
    return [
        {
            "rule": RULE_DUPLICATE_MOBBIN,
            "severity": "warning",
            "message": f"Found {len(connections)} Mobbin connection definitions.",
            "paths": [str(item.get("locator") or "") for item in connections],
            "remediation": [
                "Choose one supported Mobbin OAuth connection path.",
                "Review and disconnect obsolete Mobbin entries through their owning Codex plugin or app settings.",
            ],
        }
    ]


def _check(rule: str, findings: Sequence[Mapping[str, Any]], details: Any) -> dict[str, Any]:
    relevant = [dict(item) for item in findings if item.get("rule") == rule]
    return {
        "id": rule,
        "status": "warning" if relevant else "pass",
        "finding_count": len(relevant),
        "details": details,
    }


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
    duplicate_install_findings = _duplicate_install_findings(installs)
    drift_findings = _active_drift_findings(source, active)
    hook_records, hook_findings = _stale_hook_findings(
        codex_home,
        source,
        installs,
    )
    mobbin_connections = _mobbin_connections(codex_home, source_root, config)
    mobbin_findings = _duplicate_mobbin_findings(mobbin_connections)
    findings = (
        marketplace_findings
        + duplicate_install_findings
        + drift_findings
        + hook_findings
        + mobbin_findings
    )

    checks = [
        _check(RULE_STALE_MARKETPLACE, findings, marketplaces),
        _check(RULE_DUPLICATE_INSTALL, findings, installs),
        _check(
            RULE_ACTIVE_VERSION_DRIFT,
            findings,
            {"source": source, "active": dict(active) if active is not None else None},
        ),
        _check(RULE_STALE_HOOK_TRUST, findings, hook_records),
        _check(RULE_DUPLICATE_MOBBIN, findings, mobbin_connections),
    ]
    return {
        "schema": DOCTOR_SCHEMA,
        "verdict": "ATTENTION" if findings else "PASS",
        "read_only": True,
        "codex_home": _path_text(codex_home),
        "source_root": _path_text(source_root),
        "config": {
            "path": _path_text(config_path),
            "readable": config_error is None,
            "error": config_error,
        },
        "checks": checks,
        "finding_count": len(findings),
        "findings": findings,
    }


def doctor_exit_code(payload: Mapping[str, Any], *, strict: bool) -> int:
    if strict and payload.get("verdict") != "PASS":
        return 1
    return 0
