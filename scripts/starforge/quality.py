"""Deterministic source classification and architecture-debt analysis.

Quality scanning is deliberately separate from source hashing.  A generated or
vendored file may be omitted from quality findings while still participating in
the source hash when Git tracks it.
"""

from __future__ import annotations

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


QUALITY_REPORT_SCHEMA = "star-forge.quality.v1"
MAX_RUNTIME_MODULE_LINES = 1_200
LARGE_MODULE_WARNING_LINES = 800
MAX_CLI_MODULE_LINES = 2_500
MAX_PRODUCTION_PYTHON_LINES = 18_000
MAX_LOCAL_IMPORTS = 12

SOURCE_SUFFIXES = frozenset(
    {
        ".astro", ".c", ".cc", ".clj", ".cljs", ".cpp", ".cs", ".dart",
        ".css", ".ex", ".exs", ".fs", ".fsx", ".go", ".graphql", ".h", ".hh",
        ".hpp", ".htm", ".html", ".java",
        ".js", ".jsx", ".kt", ".kts", ".lua", ".m", ".mm", ".mjs", ".cjs",
        ".less", ".php", ".pl", ".pm", ".prisma", ".proto", ".py", ".rb", ".rs",
        ".sass", ".scala", ".scss", ".sh", ".sol", ".sql", ".swift", ".svelte",
        ".ts", ".tsx", ".vue", ".zig",
    }
)
TEXT_SUFFIXES = SOURCE_SUFFIXES | frozenset(
    {
        ".cfg", ".conf", ".css", ".graphql", ".gql", ".html", ".ini", ".json",
        ".less", ".md", ".mdx", ".mk", ".plist", ".prisma", ".proto", ".rst",
        ".sass", ".scss", ".sql", ".toml", ".txt", ".xml", ".yaml", ".yml",
    }
)
SOURCE_ROOT_NAMES = frozenset(
    {
        "app", "apps", "bin", "cli", "cmd", "components", "frontend", "include",
        "internal", "lib", "mobile", "native", "packages", "pages", "pkg",
        "routes", "scripts", "server", "services", "src", "Sources", "source",
    }
)
WORKSPACE_CONTAINER_NAMES = frozenset(
    {"apps", "packages", "services", "modules", "plugins", "crates", "workspaces"}
)
TEST_DIR_NAMES = frozenset(
    {
        "__tests__", "androidTest", "integration-tests", "spec", "specs", "test",
        "testFixtures", "testing", "tests", "Tests", "ui-tests", "uitests",
    }
)
DOC_DIR_NAMES = frozenset({"doc", "docs", "documentation"})
CONFIG_DIR_NAMES = frozenset({".github", ".circleci", "config", "configs"})
CONFIG_SUFFIXES = frozenset(
    {".cfg", ".conf", ".ini", ".json", ".plist", ".toml", ".yaml", ".yml"}
)
CONFIG_NAMES = frozenset(
    {
        "Dockerfile", "Containerfile", "Makefile", "GNUmakefile", "Procfile",
        "Caddyfile", "Package.swift", "Cargo.toml", "go.mod", "go.sum",
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "pyproject.toml", "requirements.txt", "Gemfile", "Podfile",
    }
)

# These names are unambiguous dependency or tool caches wherever they occur.
ALWAYS_EXCLUDED_DIRS: Mapping[str, str] = {
    ".codex-harness": "test harness state",
    ".git": "version-control metadata",
    ".hg": "version-control metadata",
    ".build": "native build output",
    ".bundle": "vendored dependencies",
    ".cache": "tool cache",
    ".dart_tool": "language cache",
    ".expo": "framework cache",
    ".star-forge-pycache": "language cache",
    ".starforge": "Star Forge runtime state",
    ".svn": "version-control metadata",
    ".next": "framework build output",
    ".nuxt": "framework build output",
    ".parcel-cache": "build cache",
    ".svelte-kit": "framework build output",
    ".turbo": "build cache",
    ".gradle": "build cache",
    ".pytest_cache": "test cache",
    ".mypy_cache": "analysis cache",
    ".ruff_cache": "analysis cache",
    ".tox": "test environment",
    ".venv": "virtual environment",
    "venv": "virtual environment",
    "__pycache__": "language cache",
    "node_modules": "vendored dependencies",
    "bower_components": "vendored dependencies",
    "Pods": "vendored dependencies",
    "DerivedData": "native build output",
    "the-loop": "legacy Star Forge runtime state",
}
ARTIFACT_DIRS: Mapping[str, str] = {
    "build": "build output",
    "coverage": "coverage output",
    "dist": "distribution output",
    "out": "build output",
    "obj": "compiler output",
    "target": "compiler output",
}
GENERATED_DIRS = frozenset(
    {"generated", "Generated", "generated-sources", "generatedSources", "_generated"}
)
VENDOR_DIRS = frozenset(
    {"vendor", "Vendor", "Carthage", "third_party", "third-party", "external"}
)
GENERATED_FILE_PATTERNS = (
    re.compile(r"(?:^|[._-])generated(?:[._-]|$)", re.IGNORECASE),
    re.compile(r"\.(?:g|gen)\.(?:cs|dart|go|js|kt|py|swift|ts)$", re.IGNORECASE),
    re.compile(r"(?:_pb2\.py|\.pb\.go|\.designer\.cs)$", re.IGNORECASE),
    re.compile(r"\.(?:min\.js|min\.css|map)$", re.IGNORECASE),
)
GENERATED_HEADER_RE = re.compile(
    r"(?:code\s+)?generated\b.{0,80}\b(?:do not edit|do not modify)|"
    r"\bauto[- ]generated\b",
    re.IGNORECASE | re.DOTALL,
)
TEST_FILE_RE = re.compile(
    r"(?:^test_.+|.+_test)\.(?:py|go|rb)$|"
    r"\.(?:test|spec)\.(?:[cm]?[jt]sx?|vue|svelte)$|"
    r"(?:Tests?|Spec)\.(?:swift|kt|java)$",
    re.IGNORECASE,
)
CLI_NAMES = frozenset({"cli.py", "__main__.py", "star_forge.py"})
CONTROL_PLANE_FUNCTION_RE = re.compile(
    r"^(?:read|write|load|save|parse|validate|resolve|select|collect|compute|"
    r"source|git|review|build|complete|record|merge|normalize|detect)_"
)


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
    names = {
        ".py": "python", ".js": "javascript", ".jsx": "javascript",
        ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".swift": "swift", ".kt": "kotlin",
        ".kts": "kotlin", ".java": "java", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".cs": "csharp", ".c": "c", ".cc": "cpp",
        ".cpp": "cpp", ".m": "objective-c", ".mm": "objective-cpp",
        ".dart": "dart", ".sh": "shell",
    }
    return names.get(suffix, suffix.removeprefix(".") or None)


def _workspace_artifact_position(parts: Sequence[str], index: int) -> bool:
    if index == 0:
        return True
    if index == 2 and parts[0] in WORKSPACE_CONTAINER_NAMES:
        return True
    if index == 1 and parts[0] in {"android", "ios", "macos", "windows"}:
        return True
    return False


def exclusion_reason(path: Path, project: Path | None = None) -> str | None:
    """Return a precise generated/vendor/build exclusion reason, if any."""

    relative, parts = _relative_parts(path, project)
    physical_path = (
        project.resolve() / relative
        if project is not None and not path.is_absolute()
        else path
    )
    for index, part in enumerate(parts[:-1]):
        if part in ALWAYS_EXCLUDED_DIRS:
            return ALWAYS_EXCLUDED_DIRS[part]
        if part in ARTIFACT_DIRS and _workspace_artifact_position(parts, index):
            return ARTIFACT_DIRS[part]
        if part in VENDOR_DIRS and (
            _workspace_artifact_position(parts, index) or index == 0
        ):
            return "vendored source"
        if part in GENERATED_DIRS:
            # "generated" is an explicit provenance label.  Unlike build/dist,
            # it is not applied to similar names such as generated_helpers.
            return "generated source directory"
    if any(pattern.search(relative.name) for pattern in GENERATED_FILE_PATTERNS):
        return "generated filename"
    if (
        physical_path.exists()
        and physical_path.is_file()
        and physical_path.suffix.lower() in SOURCE_SUFFIXES
    ):
        try:
            header = physical_path.read_text(
                encoding="utf-8", errors="ignore"
            )[:4096]
        except OSError:
            header = ""
        if GENERATED_HEADER_RE.search(header):
            return "generated-file header"
    return None


def is_generated_or_vendored(path: Path, project: Path | None = None) -> bool:
    return exclusion_reason(path, project) is not None


def _manifest_source_roots(project: Path) -> set[Path]:
    roots: set[Path] = set()
    profile = project / "StarForge.profile.json"
    if profile.is_file():
        try:
            payload = json.loads(profile.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        values: list[Any] = []
        if isinstance(payload, dict):
            direct = payload.get("source_roots") or payload.get("sourceRoots") or []
            if isinstance(direct, list):
                values.extend(direct)
            quality = payload.get("quality")
            if isinstance(quality, dict):
                nested = (
                    quality.get("source_roots")
                    or quality.get("sourceRoots")
                    or []
                )
                if isinstance(nested, list):
                    values.extend(nested)
        for value in values:
            if isinstance(value, str) and value.strip():
                rel = Path(value)
                if not rel.is_absolute() and ".." not in rel.parts:
                    roots.add(rel)
    pyproject = project / "pyproject.toml"
    if tomllib is not None and pyproject.is_file():
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            payload = {}
        package_dir = (
            payload.get("tool", {}).get("setuptools", {}).get("package-dir", {})
            if isinstance(payload, dict)
            else {}
        )
        if isinstance(package_dir, dict):
            for value in package_dir.values():
                if isinstance(value, str) and value and ".." not in Path(value).parts:
                    roots.add(Path(value))
    package_json = project / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        for key in ("source", "srcDir"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value and ".." not in Path(value).parts:
                roots.add(Path(value).parent if Path(value).suffix else Path(value))
    return roots


def discover_source_roots(project: Path) -> tuple[str, ...]:
    """Discover conventional and repository-declared roots deterministically."""

    project = project.resolve()
    roots = _manifest_source_roots(project)
    try:
        children = sorted(project.iterdir(), key=lambda item: item.name)
    except OSError:
        children = []
    for child in children:
        if child.is_dir() and child.name in SOURCE_ROOT_NAMES:
            roots.add(Path(child.name))
            if child.name in WORKSPACE_CONTAINER_NAMES:
                try:
                    units = sorted(child.iterdir(), key=lambda item: item.name)
                except OSError:
                    units = []
                for unit in units:
                    if unit.is_dir():
                        for nested in SOURCE_ROOT_NAMES:
                            if (unit / nested).is_dir():
                                roots.add(Path(child.name) / unit.name / nested)
    if any(
        (project / name).is_file()
        for name in ("go.mod", "Cargo.toml", "Package.swift", "pyproject.toml")
    ):
        roots.add(Path("."))
    return tuple(sorted({root.as_posix() for root in roots}))


def _matching_source_root(relative: Path, roots: Sequence[str]) -> str | None:
    matches: list[str] = []
    for root in roots:
        if root == ".":
            matches.append(root)
            continue
        root_path = Path(root)
        try:
            relative.relative_to(root_path)
        except ValueError:
            continue
        matches.append(root)
    if matches:
        return max(matches, key=lambda value: (len(Path(value).parts), value))
    parts = relative.parts[:-1]
    for index, part in enumerate(parts):
        if part in SOURCE_ROOT_NAMES:
            if part in WORKSPACE_CONTAINER_NAMES and index + 1 < len(parts):
                return Path(*parts[: index + 2]).as_posix()
            return Path(*parts[: index + 1]).as_posix()
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
    if directory_parts & TEST_DIR_NAMES or TEST_FILE_RE.search(name):
        return SourceClassification(rel, "test", language, source_root, reason="test convention")
    if directory_parts & DOC_DIR_NAMES or relative.suffix.lower() in {".md", ".mdx", ".rst"}:
        return SourceClassification(rel, "docs", language, source_root, reason="documentation convention")
    if (
        directory_parts & CONFIG_DIR_NAMES
        or name in CONFIG_NAMES
        or name.startswith(".env")
        or relative.suffix.lower() in CONFIG_SUFFIXES
    ):
        return SourceClassification(rel, "config", language, source_root, reason="configuration convention")
    if language is not None:
        return SourceClassification(rel, "production", language, source_root, reason="handwritten source")
    return SourceClassification(rel, "other", None, source_root, reason="non-source project file")


def is_source_file(path: Path, project: Path | None = None) -> bool:
    classification = classify_source_path(path, project)
    return classification.category in {"production", "test"}


def is_text_file(path: Path) -> bool:
    return (
        path.name in CONFIG_NAMES
        or path.name.startswith(".env")
        or path.suffix.lower() in TEXT_SUFFIXES
        or path.suffix.lower() in {".pem", ".key", ".crt", ".cer", ".p12", ".pfx"}
    )


def iter_project_files(project: Path) -> Iterator[Path]:
    """Yield quality-scannable files, excluding only explained artifacts."""

    project = project.resolve()
    for root, directories, filenames in os.walk(project):
        root_path = Path(root)
        kept: list[str] = []
        for name in sorted(directories):
            child = root_path / name
            if child.is_symlink() or exclusion_reason(child / "_", project):
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            path = root_path / name
            if path.is_symlink() or not path.is_file() or not is_text_file(path):
                continue
            if exclusion_reason(path, project):
                continue
            yield path


def excluded_artifacts(project: Path) -> list[dict[str, str]]:
    """List pruned artifact roots without traversing dependency/build trees."""

    project = project.resolve()
    excluded: list[dict[str, str]] = []
    for root, directories, filenames in os.walk(project):
        root_path = Path(root)
        kept: list[str] = []
        for name in sorted(directories):
            child = root_path / name
            reason = exclusion_reason(child / "_", project)
            if child.is_symlink():
                reason = "symlinked directory"
            if reason:
                excluded.append(
                    {
                        "path": child.relative_to(project).as_posix(),
                        "reason": reason,
                    }
                )
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            path = root_path / name
            reason = exclusion_reason(path, project)
            if reason:
                excluded.append(
                    {
                        "path": path.relative_to(project).as_posix(),
                        "reason": reason,
                    }
                )
    return sorted(excluded, key=lambda item: (item["path"], item["reason"]))


def classify_project(project: Path) -> list[SourceClassification]:
    roots = discover_source_roots(project)
    return [
        classify_source_path(path, project, source_roots=roots)
        for path in iter_project_files(project)
    ]


def _line_count(path: Path) -> tuple[int, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return len(text.splitlines()), text


def _finding(
    rule: str,
    severity: str,
    file: str,
    evidence: str,
    *,
    line: int = 1,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "rule": rule,
        "file": file,
        "line": line,
        "evidence": evidence,
    }


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
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")[:-1]
                trim = max(0, node.level - 1)
                if trim:
                    base = base[:-trim]
                if node.module:
                    imports.add(".".join([*base, node.module]))
                else:
                    imports.update(
                        ".".join([*base, alias.name]) for alias in node.names
                    )
            elif node.module:
                imports.add(node.module)
    return imports


def _local_import_graph(
    python_files: Mapping[str, str],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    module_to_file = {_python_module_name(rel): rel for rel in python_files}
    aliases: dict[str, str] = {}
    for module in module_to_file:
        aliases[module] = module
        first, separator, remainder = module.partition(".")
        if separator and first in SOURCE_ROOT_NAMES:
            aliases.setdefault(remainder, module)
    graph: dict[str, set[str]] = {module: set() for module in module_to_file}
    for module, rel in module_to_file.items():
        for imported in _python_imports(python_files[rel], module):
            candidates = [
                alias
                for alias in aliases
                if imported == alias or imported.startswith(alias + ".")
            ]
            if candidates:
                target = aliases[max(candidates, key=len)]
                if target != module:
                    graph[module].add(target)
    return graph, module_to_file


JS_IMPORT_RE = re.compile(
    r"(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*(?:\(\s*)?)"
    r"['\"](?P<target>\.[^'\"]+)['\"]"
)


def _script_import_graph(
    script_files: Mapping[str, str],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    module_to_file = {
        Path(rel).with_suffix("").as_posix(): rel for rel in script_files
    }
    aliases: dict[str, str] = {}
    for module in module_to_file:
        aliases[module] = module
        aliases[module + "/index"] = module
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
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while stack:
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
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and CONTROL_PLANE_FUNCTION_RE.match(node.name)
    }


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
        if path.suffix.lower() == ".py":
            production_python_lines += count
            python_files[rel] = text
            for function in _top_level_functions(text):
                functions[function].append(rel)
        elif path.suffix.lower() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
            script_files[rel] = text
        if count > MAX_RUNTIME_MODULE_LINES:
            findings.append(
                _finding(
                    "architecture-debt-large-file",
                    "medium",
                    rel,
                    f"{count} lines exceeds the {MAX_RUNTIME_MODULE_LINES}-line runtime module budget",
                )
            )
        elif count > LARGE_MODULE_WARNING_LINES:
            findings.append(
                _finding(
                    "architecture-debt-large-file",
                    "low",
                    rel,
                    f"{count} lines approaches the {MAX_RUNTIME_MODULE_LINES}-line runtime module budget",
                )
            )
        ts_ignores = (
            len(re.findall(r"@ts-ignore", text))
            if path.suffix.lower() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
            else 0
        )
        if ts_ignores:
            findings.append(
                _finding(
                    "architecture-debt-ts-ignore",
                    "high",
                    rel,
                    f"{ts_ignores} @ts-ignore directive(s); justify or remove",
                )
            )

    cli_files = [
        (rel, count)
        for rel, _path, count, _text in production
        if Path(rel).name in CLI_NAMES
        or any(part in {"cli", "cmd", "bin"} for part in Path(rel).parts[:-1])
    ]
    for rel, count in cli_files:
        if count > MAX_CLI_MODULE_LINES:
            findings.append(
                _finding(
                    "architecture-debt-cli-concentration",
                    "medium",
                    rel,
                    f"{count} lines exceeds the approved {MAX_CLI_MODULE_LINES}-line CLI budget",
                )
            )
    total_production_lines = sum(item[2] for item in production)
    total_cli_lines = sum(count for _rel, count in cli_files)
    if (
        cli_files
        and not any(count > MAX_CLI_MODULE_LINES for _rel, count in cli_files)
        and total_cli_lines >= LARGE_MODULE_WARNING_LINES
        and total_cli_lines * 5 >= total_production_lines * 3
    ):
        findings.append(
            _finding(
                "architecture-debt-cli-concentration",
                "low",
                sorted(rel for rel, _count in cli_files)[0],
                f"{total_cli_lines} of {total_production_lines} production lines "
                "are concentrated in CLI entry points",
            )
        )
    if production_python_lines > MAX_PRODUCTION_PYTHON_LINES:
        findings.append(
            _finding(
                "architecture-debt-python-budget",
                "medium",
                ".",
                f"{production_python_lines} production Python lines exceeds the approved "
                f"{MAX_PRODUCTION_PYTHON_LINES}-line budget",
            )
        )

    graph, module_to_file = _local_import_graph(python_files)
    script_graph, script_module_to_file = _script_import_graph(script_files)
    for language_graph, file_map in (
        (graph, module_to_file),
        (script_graph, script_module_to_file),
    ):
        for component in _strongly_connected(language_graph):
            files = sorted(file_map[module] for module in component)
            findings.append(
                _finding(
                    "architecture-debt-import-cycle",
                    "medium",
                    files[0],
                    "local import cycle: " + " -> ".join([*files, files[0]]),
                )
            )
        for module, imports in sorted(language_graph.items()):
            if len(imports) > MAX_LOCAL_IMPORTS:
                findings.append(
                    _finding(
                        "architecture-debt-coupling",
                        "low",
                        file_map[module],
                        f"{len(imports)} local module dependencies exceeds the "
                        f"{MAX_LOCAL_IMPORTS}-dependency coupling signal",
                    )
                )
    for function, files in sorted(functions.items()):
        unique_files = sorted(set(files))
        if len(unique_files) < 2:
            continue
        findings.append(
            _finding(
                "architecture-debt-duplicated-responsibility",
                "low",
                unique_files[0],
                f"top-level control-plane function {function} is duplicated across "
                + ", ".join(unique_files),
            )
        )
    return sorted(
        findings,
        key=lambda item: (
            item["rule"], item["file"], int(item.get("line", 0)), item["evidence"]
        ),
    )


def quality_report(project: Path, *, include_files: bool = False) -> dict[str, Any]:
    """Build a deterministic, explainable project quality report."""

    project = project.resolve()
    paths = list(iter_project_files(project))
    classifications = classify_project(project)
    counts = Counter(item.category for item in classifications)
    findings = architecture_debt_findings(paths, project)
    payload: dict[str, Any] = {
        "schema": QUALITY_REPORT_SCHEMA,
        "project": ".",
        "source_roots": list(discover_source_roots(project)),
        "classification_counts": dict(sorted(counts.items())),
        "scanned_files": len(paths),
        "excluded_artifacts": excluded_artifacts(project),
        "budgets": {
            "runtime_module_lines": MAX_RUNTIME_MODULE_LINES,
            "cli_module_lines": MAX_CLI_MODULE_LINES,
            "production_python_lines": MAX_PRODUCTION_PYTHON_LINES,
        },
        "findings": findings,
    }
    if include_files:
        payload["files"] = [item.to_dict() for item in classifications]
    return payload


__all__ = [
    "LARGE_MODULE_WARNING_LINES",
    "MAX_CLI_MODULE_LINES",
    "MAX_PRODUCTION_PYTHON_LINES",
    "MAX_RUNTIME_MODULE_LINES",
    "QUALITY_REPORT_SCHEMA",
    "SOURCE_SUFFIXES",
    "SourceClassification",
    "architecture_debt_findings",
    "classify_project",
    "classify_source_path",
    "discover_source_roots",
    "excluded_artifacts",
    "exclusion_reason",
    "is_generated_or_vendored",
    "is_source_file",
    "is_text_file",
    "iter_project_files",
    "quality_report",
]
