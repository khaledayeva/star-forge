"""Deterministic source classification and architecture-debt analysis.

Quality scanning is deliberately separate from source hashing.  A generated or
vendored file may be omitted from quality findings while still participating in
the source hash when Git tracks it.
"""

from __future__ import annotations
from .policy_data import mapping as _policy_mapping, value as _policy_value

import ast
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    tomllib = None  # type: ignore[assignment]
_POLICY = _policy_value("quality.POLICY")
QUALITY_REPORT_SCHEMA = _POLICY["schema"]
MAX_RUNTIME_MODULE_LINES = _POLICY["budgets"]["runtime_module_lines"]
LARGE_MODULE_WARNING_LINES = _POLICY["budgets"]["large_module_warning_lines"]
MAX_CLI_MODULE_LINES = _POLICY["budgets"]["cli_module_lines"]
MAX_PRODUCTION_PYTHON_LINES = _POLICY["budgets"]["production_python_lines"]
MAX_LOCAL_IMPORTS = _POLICY["budgets"]["local_imports"]
SOURCE_SUFFIXES = _policy_value('quality.SOURCE_SUFFIXES')
TEXT_SUFFIXES = SOURCE_SUFFIXES | frozenset(_POLICY["text_suffixes"])
SOURCE_ROOT_NAMES = _policy_value('quality.SOURCE_ROOT_NAMES')
WORKSPACE_CONTAINER_NAMES = frozenset(_POLICY["workspace_containers"])
TEST_DIR_NAMES = _policy_value('quality.TEST_DIR_NAMES')
DOC_DIR_NAMES = frozenset(_POLICY["docs_dirs"])
CONFIG_DIR_NAMES = frozenset(_POLICY["config_dirs"])
CONFIG_SUFFIXES = frozenset(_POLICY["config_suffixes"])
CONFIG_NAMES = _policy_value('quality.CONFIG_NAMES')
ALWAYS_EXCLUDED_DIRS: Mapping[str, str] = _policy_value('quality.ALWAYS_EXCLUDED_DIRS')
ARTIFACT_DIRS: Mapping[str, str] = _policy_value('quality.ARTIFACT_DIRS')
GENERATED_DIRS = frozenset(_POLICY["generated_dirs"])
VENDOR_DIRS = frozenset(_POLICY["vendor_dirs"])
GENERATED_FILE_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE)
                                for pattern in _POLICY["generated_file_patterns"])
GENERATED_HEADER_RE = re.compile(_POLICY["generated_header_pattern"], re.IGNORECASE | re.DOTALL)
TEST_FILE_RE = re.compile(_POLICY["test_file_pattern"], re.IGNORECASE)
CLI_NAMES = frozenset(_POLICY["cli_names"])
CONTROL_PLANE_FUNCTION_RE = re.compile(_POLICY["control_plane_function_pattern"])

@dataclass(frozen=True)
class SourceClassification:
    """One explainable classification result."""
    path: str
    category: str
    language: str | None
    source_root: str | None
    excluded: bool = False
    reason: str = ""
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def _relative_parts(path: Path, project: Path | None) -> tuple[Path, tuple[str, ...]]:
    candidate = path
    if project is not None:
        project = project.resolve()
        if path.is_absolute():
            try:
                candidate = path.resolve().relative_to(project)
            except (OSError, ValueError):
                candidate = Path(path.name)
    return candidate, tuple(part for part in candidate.parts if part not in {"", "."})

def _language(path: Path) -> str | None:
    suffix = path.suffix.lower()
    return _POLICY["language_by_suffix"].get(suffix, suffix.removeprefix(".") or None)

def _workspace_artifact_position(parts: Sequence[str], index: int) -> bool:
    return (index == 0
            or index == 2 and parts[0] in WORKSPACE_CONTAINER_NAMES
            or index == 1 and parts[0] in _POLICY["platform_roots"])

def exclusion_reason(path: Path, project: Path | None = None) -> str | None:
    """Return a precise generated/vendor/build exclusion reason, if any."""
    relative, parts = _relative_parts(path, project)
    physical_path = (project.resolve() / relative if project is not None and not path.is_absolute() else path)
    for index, part in enumerate(parts[:-1]):
        if part in ALWAYS_EXCLUDED_DIRS:
            return ALWAYS_EXCLUDED_DIRS[part]
        if part in ARTIFACT_DIRS and _workspace_artifact_position(parts, index):
            return ARTIFACT_DIRS[part]
        if part in VENDOR_DIRS and (_workspace_artifact_position(parts, index) or index == 0):
            return "vendored source"
        if part in GENERATED_DIRS:
            # "generated" is an explicit provenance label.  Unlike build/dist,
            # it is not applied to similar names such as generated_helpers.
            return "generated source directory"
    if any(pattern.search(relative.name) for pattern in GENERATED_FILE_PATTERNS):
        return "generated filename"
    if (physical_path.exists() and physical_path.is_file() and physical_path.suffix.lower() in SOURCE_SUFFIXES):
        try:
            header = physical_path.read_text(encoding="utf-8", errors="ignore")[
                :_POLICY["generated_header_limit"]]
        except OSError:
            header = ""
        if GENERATED_HEADER_RE.search(header):
            return "generated-file header"
    return None

def is_generated_or_vendored(path: Path, project: Path | None = None) -> bool:
    return exclusion_reason(path, project) is not None

def _json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _add_declared_roots(roots: set[Path], values: Any, *, file_value: bool = False) -> None:
    values = values if isinstance(values, list) else [values]
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        root = Path(value)
        if not root.is_absolute() and ".." not in root.parts:
            roots.add(root.parent if file_value and root.suffix else root)

def _manifest_source_roots(project: Path) -> set[Path]:
    roots: set[Path] = set()
    profile = _json_object(project / "StarForge.profile.json")
    quality = profile.get("quality")
    for source in (profile, quality if isinstance(quality, dict) else {}):
        for key in _POLICY["profile_root_keys"]:
            if key in source:
                _add_declared_roots(roots, source[key])
    pyproject = project / "pyproject.toml"
    if tomllib is not None and pyproject.is_file():
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            payload = {}
        package_dir = (payload.get("tool", {}).get("setuptools", {}).get("package-dir", {}) if isinstance(payload, dict) else {})
        if isinstance(package_dir, dict):
            _add_declared_roots(roots, list(package_dir.values()))
    package = _json_object(project / "package.json")
    for key in _POLICY["package_root_keys"]:
        if key in package:
            _add_declared_roots(roots, package[key], file_value=True)
    return roots

def discover_source_roots(project: Path) -> tuple[str, ...]:
    """Discover conventional and repository-declared roots deterministically."""
    project = project.resolve()
    roots = _manifest_source_roots(project)
    try:
        children = sorted((path for path in project.iterdir() if path.is_dir()),
                          key=lambda item: item.name)
    except OSError:
        children = []
    for child in children:
        if child.name in SOURCE_ROOT_NAMES:
            roots.add(Path(child.name))
            if child.name in WORKSPACE_CONTAINER_NAMES:
                try:
                    units = sorted((path for path in child.iterdir() if path.is_dir()),
                                   key=lambda item: item.name)
                except OSError:
                    units = []
                for unit in units:
                    for nested in SOURCE_ROOT_NAMES:
                        if (unit / nested).is_dir():
                            roots.add(Path(child.name) / unit.name / nested)
    if any((project / name).is_file() for name in _POLICY["root_markers"]):
        roots.add(Path("."))
    return tuple(sorted({root.as_posix() for root in roots}))

def _matching_source_root(relative: Path, roots: Sequence[str]) -> str | None:
    matches = [root for root in roots
               if root == "." or relative.is_relative_to(Path(root))]
    if matches:
        return max(matches, key=lambda value: (len(Path(value).parts), value))
    parts = relative.parts[:-1]
    for index, part in enumerate(parts):
        if part in SOURCE_ROOT_NAMES:
            if part in WORKSPACE_CONTAINER_NAMES and index + 1 < len(parts):
                return Path(*parts[:index + 2]).as_posix()
            return Path(*parts[:index + 1]).as_posix()
    return "." if relative.suffix.lower() in SOURCE_SUFFIXES else None

def classify_source_path(
    path: Path | str,
    project: Path | str | None = None,
    *,
    source_roots: Sequence[str] | None = None,
) -> SourceClassification:
    """Classify a path as production, test, docs, config, other, or excluded."""
    candidate = Path(path)
    root = Path(project).resolve() if project is not None else None
    relative, parts = _relative_parts(candidate, root)
    rel = relative.as_posix()
    reason = exclusion_reason(candidate, root)
    language = _language(relative) if relative.suffix.lower() in SOURCE_SUFFIXES else None
    roots = tuple(source_roots or (discover_source_roots(root) if root else ()))
    source_root = _matching_source_root(relative, roots)
    if reason:
        return SourceClassification(rel, "excluded", language, source_root, True, reason)
    directory_parts = set(parts[:-1])
    name = relative.name
    matches = (
        bool(directory_parts & TEST_DIR_NAMES or TEST_FILE_RE.search(name)),
        bool(directory_parts & DOC_DIR_NAMES or relative.suffix.lower() in _POLICY["docs_suffixes"]),
        bool(directory_parts & CONFIG_DIR_NAMES or name in CONFIG_NAMES or name.startswith(".env")
             or relative.suffix.lower() in CONFIG_SUFFIXES),
        language is not None,
        True,
    )
    for (category, explanation), matched in zip(_POLICY["classification_rules"], matches):
        if matched:
            return SourceClassification(rel, category, language, source_root, reason=explanation)
    raise AssertionError("quality classification policy has no fallback")

def is_source_file(path: Path, project: Path | None = None) -> bool:
    classification = classify_source_path(path, project)
    return classification.category in {"production", "test"}

def is_text_file(path: Path) -> bool:
    return (path.name in CONFIG_NAMES or path.name.startswith(".env") or path.suffix.lower() in TEXT_SUFFIXES or
            path.suffix.lower() in _POLICY["sensitive_text_suffixes"])

def _walk_project(project: Path) -> Iterator[tuple[Path, str | None, bool]]:
    project = project.resolve()
    for root, directories, filenames in os.walk(project):
        root_path = Path(root)
        kept: list[str] = []
        for name in sorted(directories):
            child = root_path / name
            reason = ("symlinked directory" if child.is_symlink()
                      else exclusion_reason(child / "_", project))
            if reason:
                yield child, reason, True
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            path = root_path / name
            yield path, exclusion_reason(path, project), False

def iter_project_files(project: Path) -> Iterator[Path]:
    """Yield quality-scannable files, excluding only explained artifacts."""
    for path, reason, is_directory in _walk_project(project):
        if (not is_directory and not reason and not path.is_symlink()
                and path.is_file() and is_text_file(path)):
            yield path

def excluded_artifacts(project: Path) -> list[dict[str, str]]:
    """List pruned artifact roots without traversing dependency/build trees."""
    project = project.resolve()
    excluded = [{"path": path.relative_to(project).as_posix(), "reason": reason}
                for path, reason, _is_directory in _walk_project(project) if reason]
    return sorted(excluded, key=lambda item: (item["path"], item["reason"]))

def classify_project(project: Path) -> list[SourceClassification]:
    roots = discover_source_roots(project)
    return [classify_source_path(path, project, source_roots=roots) for path in iter_project_files(project)]

def _line_count(path: Path) -> tuple[int, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return len(text.splitlines()), text

def _finding(kind: str, file: str, *, line: int = 1, **facts: Any) -> dict[str, Any]:
    descriptor = _POLICY["findings"][kind]
    values = {"severity": descriptor["severity"], "rule": descriptor["rule"],
              "file": file, "line": line, "evidence": descriptor["evidence"].format(**facts)}
    return {field: values[field] for field in _POLICY["finding_fields"]}

def _python_module_name(rel: str) -> str:
    path = Path(rel)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)

def _python_imports(text: str, module: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = module.split(".")[:-node.level]
            names = [node.module] if node.module else [alias.name for alias in node.names]
            imports.update(".".join([*base, name]) for name in names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports

def _local_import_graph(python_files: Mapping[str, str], ) -> tuple[dict[str, set[str]], dict[str, str]]:
    module_to_file = {_python_module_name(rel): rel for rel in python_files}
    aliases = {remainder: module for module in reversed(module_to_file)
                    for first, separator, remainder in [module.partition(".")]
                    if separator and first in SOURCE_ROOT_NAMES}
    aliases.update({module: module for module in module_to_file})
    graph: dict[str, set[str]] = {module: set() for module in module_to_file}
    for module, rel in module_to_file.items():
        for imported in _python_imports(python_files[rel], module):
            candidates = [alias for alias in aliases if imported == alias or imported.startswith(alias + ".")]
            if candidates:
                target = aliases[max(candidates, key=len)]
                if target != module:
                    graph[module].add(target)
    return graph, module_to_file
JS_IMPORT_RE = re.compile(r"(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*(?:\(\s*)?)"
                          r"['\"](?P<target>\.[^'\"]+)['\"]")

def _script_import_graph(script_files: Mapping[str, str], ) -> tuple[dict[str, set[str]], dict[str, str]]:
    module_to_file = {Path(rel).with_suffix("").as_posix(): rel for rel in script_files}
    aliases = {alias: module for module in module_to_file
               for alias in (module, module + "/index")}
    graph: dict[str, set[str]] = {module: set() for module in module_to_file}
    for module, rel in module_to_file.items():
        parent = Path(module).parent
        for match in JS_IMPORT_RE.finditer(script_files[rel]):
            target = (parent / match.group("target")).as_posix()
            while target.startswith("./"):
                target = target[2:]
            normalized = Path(os.path.normpath(target)).as_posix()
            resolved = aliases.get(normalized) or aliases.get(normalized + "/index")
            if resolved and resolved != module:
                graph[module].add(resolved)
    return graph, module_to_file

def _strongly_connected(graph: Mapping[str, set[str]]) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    active: set[str] = set()
    components: list[tuple[str, ...]] = []
    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in sorted(graph.get(node, set())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in active:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            active.remove(target)
            component.append(target)
            if target == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))
    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components)

def _top_level_functions(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and CONTROL_PLANE_FUNCTION_RE.match(node.name)}

def _module_findings(rel: str, path: Path, count: int, text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if count > MAX_RUNTIME_MODULE_LINES:
        findings.append(_finding("large_file", rel, count=count, limit=MAX_RUNTIME_MODULE_LINES))
    elif count > LARGE_MODULE_WARNING_LINES:
        findings.append(_finding("large_file_warning", rel, count=count,
                                 limit=MAX_RUNTIME_MODULE_LINES))
    if path.suffix.lower() in _POLICY["script_suffixes"]:
        ignores = len(re.findall(r"@ts-ignore", text))
        if ignores:
            findings.append(_finding("ts_ignore", rel, count=ignores))
    return findings

def _graph_findings(
    graph: Mapping[str, set[str]],
    file_map: Mapping[str, str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for component in _strongly_connected(graph):
        files = sorted(file_map[module] for module in component)
        findings.append(_finding("import_cycle", files[0],
                                 cycle=" -> ".join([*files, files[0]])))
    for module, imports in sorted(graph.items()):
        if len(imports) > MAX_LOCAL_IMPORTS:
            findings.append(_finding("coupling", file_map[module], count=len(imports),
                                     limit=MAX_LOCAL_IMPORTS))
    return findings

def _cli_findings(production: Sequence[tuple[str, Path, int, str]]) -> list[dict[str, Any]]:
    cli_files = [(rel, count) for rel, _path, count, _text in production
                 if Path(rel).name in CLI_NAMES
                 or any(part in _POLICY["cli_dirs"] for part in Path(rel).parts[:-1])]
    findings = [_finding("cli_budget", rel, count=count, limit=MAX_CLI_MODULE_LINES)
                for rel, count in cli_files if count > MAX_CLI_MODULE_LINES]
    total_lines = sum(item[2] for item in production)
    cli_lines = sum(count for _rel, count in cli_files)
    concentrated = (cli_files and not findings and cli_lines >= LARGE_MODULE_WARNING_LINES
                    and cli_lines * 5 >= total_lines * 3)
    if concentrated:
        findings.append(_finding("cli_concentration", min(rel for rel, _ in cli_files),
                                 cli_lines=cli_lines, total_lines=total_lines))
    return findings

def architecture_debt_findings(
    paths: Iterable[Path],
    project: Path,
) -> list[dict[str, Any]]:
    """Report deterministic module, CLI, coupling, duplication, and budget debt."""
    project = project.resolve()
    roots = discover_source_roots(project)
    findings: list[dict[str, Any]] = []
    python_files: dict[str, str] = {}
    script_files: dict[str, str] = {}
    production_python_lines = 0
    functions: dict[str, list[str]] = defaultdict(list)
    production: list[tuple[str, Path, int, str]] = []
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        classification = classify_source_path(path, project, source_roots=roots)
        if classification.category != "production":
            continue
        counted = _line_count(path)
        if counted is None:
            continue
        count, text = counted
        rel = classification.path
        production.append((rel, path, count, text))
        findings.extend(_module_findings(rel, path, count, text))
        if path.suffix.lower() == ".py":
            if classification.source_root in roots:
                production_python_lines += count
            python_files[rel] = text
            for function in _top_level_functions(text):
                functions[function].append(rel)
        elif path.suffix.lower() in _POLICY["script_suffixes"]:
            script_files[rel] = text
    findings.extend(_cli_findings(production))
    if production_python_lines > MAX_PRODUCTION_PYTHON_LINES:
        findings.append(_finding("python_budget", ".", count=production_python_lines,
                                 limit=MAX_PRODUCTION_PYTHON_LINES))
    graph, module_to_file = _local_import_graph(python_files)
    script_graph, script_module_to_file = _script_import_graph(script_files)
    findings.extend(_graph_findings(graph, module_to_file))
    findings.extend(_graph_findings(script_graph, script_module_to_file))
    for function, files in sorted(functions.items()):
        unique_files = sorted(set(files))
        if len(unique_files) >= 2:
            findings.append(_finding("duplicated_responsibility", unique_files[0],
                                     function=function, files=", ".join(unique_files)))
    order = _POLICY["finding_order"]
    return sorted(findings, key=lambda item: tuple(item[field] for field in order))

def quality_report(project: Path, *, include_files: bool = False) -> dict[str, Any]:
    """Build a deterministic, explainable project quality report."""
    project = project.resolve()
    roots = discover_source_roots(project)
    paths = list(iter_project_files(project))
    classifications = [classify_source_path(path, project, source_roots=roots)
                       for path in paths]
    counts = Counter(item.category for item in classifications)
    budgets = _policy_mapping(
        "quality_budgets", runtime_module_lines=MAX_RUNTIME_MODULE_LINES,
        cli_module_lines=MAX_CLI_MODULE_LINES,
        production_python_lines=MAX_PRODUCTION_PYTHON_LINES)
    payload = _policy_mapping(
        "quality_report", source_roots=list(roots),
        classification_counts=dict(sorted(counts.items())), scanned_files=len(paths),
        excluded_artifacts=excluded_artifacts(project), budgets=budgets,
        findings=architecture_debt_findings(paths, project))
    if include_files:
        payload["files"] = [item.to_dict() for item in classifications]
    return payload
