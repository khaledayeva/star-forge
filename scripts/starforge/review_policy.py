"""Deterministic adaptive review-role selection for Star Forge.

The policy consumes only structured Blueprint fields and Plan proof kinds. It
does not scan product prose or file names for suggestive keywords. This keeps
the review wave reproducible while preserving the v0.3 profile mapping for
Blueprints that predate the structured Risk Flags contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


REVIEW_POLICY_SCHEMA = "star-forge.review-policy.v1"
MAX_REVIEW_AGENTS = 4

LEGACY_PROFILE_ROLES: dict[str, tuple[str, ...]] = {
    "standard": ("correctness", "security", "architecture"),
    "fast-mvp": ("correctness",),
}

ROLE_LENSES: dict[str, str] = {
    "correctness": (
        "functional correctness, regressions, edge cases, and "
        "acceptance-criteria coverage"
    ),
    "ux-accessibility": (
        "user experience, interaction quality, accessibility, responsive "
        "behavior, and visual regressions"
    ),
    "security": (
        "security, privacy, secrets, injection, auth, unsafe IO, and "
        "dependency exposure"
    ),
    "architecture": (
        "maintainability, coupling, data flow, boundaries, and future change safety"
    ),
    "performance-reliability": (
        "latency, resource use, responsiveness, resilience, failure recovery, "
        "and operational reliability"
    ),
    "architecture-performance-reliability": (
        "architecture, coupling, data flow, performance, resource use, "
        "resilience, and operational reliability"
    ),
}

ALL_REVIEW_ROLES = frozenset(ROLE_LENSES)

RISK_FLAG_ORDER = (
    "User-facing UI",
    "Authentication or authorization",
    "Payments or financial data",
    "Secrets or privileged operations",
    "Network access or external input",
    "User, sensitive, or regulated data",
    "Privacy obligations",
    "Security-sensitive behavior",
    "Meaningful dependency exposure",
    "Multiple services or high coupling",
    "Migrations or complex persistence",
    "Performance or reliability constraints",
    "Destructive operations",
)

UI_FLAGS = ("User-facing UI",)
SECURITY_PRIVACY_FLAGS = (
    "Authentication or authorization",
    "Payments or financial data",
    "Secrets or privileged operations",
    "Network access or external input",
    "User, sensitive, or regulated data",
    "Privacy obligations",
    "Security-sensitive behavior",
    "Meaningful dependency exposure",
    "Destructive operations",
)
ARCHITECTURE_FLAGS = (
    "Multiple services or high coupling",
    "Migrations or complex persistence",
)
PERFORMANCE_RELIABILITY_FLAGS = ("Performance or reliability constraints",)

_RISK_FLAG_LOOKUP = {name.casefold(): name for name in RISK_FLAG_ORDER}
_RESOLVED_FLAG_VALUES = {
    "yes": "yes",
    "no": "no",
    "not applicable": "not applicable",
    "n/a": "not applicable",
    "na": "not applicable",
}
_UNRESOLVED_VALUES = {
    "",
    "-",
    "tbd",
    "todo",
    "unknown",
    "unresolved",
    "none",
}
_PROJECT_CLASS_SURFACES: dict[str, tuple[str, ...]] = {
    "web": ("web",),
    "web-app": ("web", "ui"),
    "website": ("web", "ui"),
    "frontend": ("web", "ui"),
    "nextjs": ("web", "ui"),
    "react": ("web", "ui"),
    "ios": ("native-mobile", "ui"),
    "ios-app": ("native-mobile", "ui"),
    "android": ("native-mobile", "ui"),
    "android-app": ("native-mobile", "ui"),
    "mobile": ("native-mobile", "ui"),
    "mobile-app": ("native-mobile", "ui"),
    "react-native": ("native-mobile", "ui"),
    "expo": ("native-mobile", "ui"),
    "macos": ("native-desktop", "ui"),
    "macos-app": ("native-desktop", "ui"),
    "desktop": ("native-desktop", "ui"),
    "desktop-app": ("native-desktop", "ui"),
    "cli": ("cli",),
    "command-line": ("cli",),
    "library": ("library",),
    "package": ("library",),
    "api": ("service",),
    "service": ("service",),
    "backend": ("service",),
    "embedded": ("embedded",),
    "realtime": ("realtime",),
    "real-time": ("realtime",),
}
_PLATFORM_SURFACES: dict[str, tuple[str, ...]] = {
    "web": ("web",),
    "browser": ("web", "ui"),
    "ios": ("native-mobile", "ui"),
    "ipados": ("native-mobile", "ui"),
    "watchos": ("native-mobile", "ui"),
    "android": ("native-mobile", "ui"),
    "expo": ("native-mobile", "ui"),
    "react-native": ("native-mobile", "ui"),
    "macos": ("native-desktop", "ui"),
    "windows": ("native-desktop", "ui"),
    "linux-desktop": ("native-desktop", "ui"),
    "visionos": ("native-mobile", "ui"),
    "tvos": ("native-mobile", "ui"),
    "embedded": ("embedded",),
    "realtime": ("realtime",),
    "real-time": ("realtime",),
}
_PROOF_SURFACES: dict[str, tuple[str, ...]] = {
    "browser": ("web", "ui"),
    "native-ios": ("native-mobile", "ui"),
    "native-macos": ("native-desktop", "ui"),
    "preview": ("web",),
}
_PLATFORM_PERFORMANCE_SURFACES = frozenset(
    {"native-desktop", "native-mobile", "embedded", "realtime"}
)
_RELIABILITY_DELIVERY_TARGETS = frozenset({"production"})


@dataclass(frozen=True)
class RiskFlag:
    """One normalized Risk Flags table row."""

    name: str
    value: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ProjectSurfaces:
    """Structured project facts that may establish review applicability."""

    project_classes: tuple[str, ...] = ()
    target_platforms: tuple[str, ...] = ()
    proof_kinds: tuple[str, ...] = ()
    delivery_targets: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_classes": list(self.project_classes),
            "target_platforms": list(self.target_platforms),
            "proof_kinds": list(self.proof_kinds),
            "delivery_targets": list(self.delivery_targets),
            "surfaces": list(self.surfaces),
        }


@dataclass(frozen=True)
class ReviewRoleSelection:
    """One agent role, its logical lenses, and explicit applicability reasons."""

    role: str
    lenses: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "lenses": list(self.lenses),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ReviewPolicySelection:
    """The complete deterministic and optionally source-bound review decision."""

    legacy: bool
    profile: str
    source_hash: str | None
    project_surfaces: ProjectSurfaces
    risk_flags: tuple[RiskFlag, ...]
    selections: tuple[ReviewRoleSelection, ...]
    combined: bool

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(item.role for item in self.selections)

    def reasons_for(self, role: str) -> tuple[str, ...]:
        item = next(
            (selection for selection in self.selections if selection.role == role),
            None,
        )
        return item.reasons if item else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REVIEW_POLICY_SCHEMA,
            "legacy": self.legacy,
            "profile": self.profile,
            "source_hash": self.source_hash,
            "max_agents": MAX_REVIEW_AGENTS,
            "combined": self.combined,
            "project_surfaces": self.project_surfaces.to_dict(),
            "risk_flags": {
                flag.name: flag.to_dict()
                for flag in self.risk_flags
            },
            "roles": list(self.roles),
            "selections": [
                selection.to_dict() for selection in self.selections
            ],
        }


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")


def _deduplicated(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def _markdown_section(text: str, title: str) -> str:
    lines = text.splitlines()
    wanted = title.strip().casefold()
    start = -1
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match and match.group(2).strip().casefold() == wanted:
            start = index + 1
            level = len(match.group(1))
            break
    if start < 0:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        match = re.match(r"^\s*(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    cell: list[str] = []
    escaped = False
    for char in stripped:
        if escaped:
            cell.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
    cells.append("".join(cell).strip())
    return cells


def _clean_cell(value: str) -> str:
    cleaned = value.strip().strip("`").strip()
    cleaned = re.sub(r"^\*\*(.*?)\*\*$", r"\1", cleaned)
    return cleaned.strip()


def _risk_flags(text: str) -> tuple[RiskFlag, ...]:
    section = _markdown_section(text, "Risk Flags")
    values: dict[str, set[str]] = {}
    reasons: dict[str, set[str]] = {}
    for line in section.splitlines():
        cells = _table_cells(line)
        if len(cells) < 2:
            continue
        raw_name = _clean_cell(cells[0])
        name = _RISK_FLAG_LOOKUP.get(raw_name.casefold())
        if not name:
            continue
        raw_value = _clean_cell(cells[1])
        normalized_value = _RESOLVED_FLAG_VALUES.get(raw_value.casefold())
        if normalized_value is None:
            normalized_value = (
                "unresolved"
                if raw_value.casefold() in _UNRESOLVED_VALUES
                or bool(re.fullmatch(r"<[^>]+>", raw_value))
                else "invalid"
            )
        values.setdefault(name, set()).add(normalized_value)
        if len(cells) >= 3:
            reason = _clean_cell(cells[2])
            if reason and not re.fullmatch(r"<[^>]+>", reason):
                reasons.setdefault(name, set()).add(reason)

    result: list[RiskFlag] = []
    precedence = ("yes", "invalid", "unresolved", "no", "not applicable")
    for name in RISK_FLAG_ORDER:
        observed = values.get(name)
        if not observed:
            continue
        value = next(item for item in precedence if item in observed)
        result.append(
            RiskFlag(
                name=name,
                value=value,
                reasons=tuple(sorted(reasons.get(name, set()))),
            )
        )
    return tuple(result)


def _field_values(text: str, name: str) -> tuple[str, ...]:
    values: list[str] = []
    for match in re.finditer(
        rf"^\s*[-*]\s*(?:\*\*)?{re.escape(name)}(?:\*\*)?\s*:\s*(.*?)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        raw = _clean_cell(match.group(1))
        if (
            not raw
            or re.fullmatch(r"<[^>]+>", raw)
            or raw.casefold() in _UNRESOLVED_VALUES | {"not applicable", "n/a", "na"}
        ):
            continue
        for item in re.split(r"[,;/]|\s+\band\b\s+", raw, flags=re.IGNORECASE):
            normalized = _normalized(item)
            if normalized:
                values.append(normalized)
    return _deduplicated(values)


def _proof_kinds(tasks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    values: list[str] = []
    for task in tasks:
        raw = task.get("proof")
        if isinstance(raw, str):
            items = re.split(r"[,;]", raw)
        elif isinstance(raw, Sequence):
            items = [str(item) for item in raw]
        else:
            items = []
        values.extend(_normalized(item) for item in items if _normalized(item))
    return tuple(sorted(set(values)))


def _surface_values(
    project_classes: Sequence[str],
    target_platforms: Sequence[str],
    proof_kinds: Sequence[str],
) -> tuple[str, ...]:
    surfaces: set[str] = set()
    for value in project_classes:
        surfaces.update(_PROJECT_CLASS_SURFACES.get(value, ()))
    for value in target_platforms:
        surfaces.update(_PLATFORM_SURFACES.get(value, ()))
    for value in proof_kinds:
        surfaces.update(_PROOF_SURFACES.get(value, ()))
    return tuple(sorted(surfaces))


def parse_project_surfaces(
    blueprint_text: str,
    tasks: Sequence[Mapping[str, Any]] = (),
) -> ProjectSurfaces:
    """Parse only canonical Blueprint fields and Plan proof cells."""

    project_classes = _field_values(blueprint_text, "Project class")
    target_platforms = _field_values(blueprint_text, "Target platforms")
    delivery_targets = _field_values(blueprint_text, "Delivery target")
    proof_kinds = _proof_kinds(tasks)
    return ProjectSurfaces(
        project_classes=project_classes,
        target_platforms=target_platforms,
        proof_kinds=proof_kinds,
        delivery_targets=delivery_targets,
        surfaces=_surface_values(project_classes, target_platforms, proof_kinds),
    )


def _flag_reasons(
    flags: Mapping[str, RiskFlag],
    names: Sequence[str],
) -> tuple[str, ...]:
    reasons: list[str] = []
    for name in names:
        flag = flags.get(name)
        if flag is None or flag.value != "yes":
            continue
        suffix = f": {'; '.join(flag.reasons)}" if flag.reasons else ""
        reasons.append(f"Risk flag `{name}` is yes{suffix}")
    return tuple(reasons)


def _surface_reasons(
    surfaces: ProjectSurfaces,
    *,
    lens: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if lens == "ux-accessibility" and "ui" in surfaces.surfaces:
        ui_proofs = [
            proof
            for proof in surfaces.proof_kinds
            if "ui" in _PROOF_SURFACES.get(proof, ())
        ]
        ui_classes = [
            item
            for item in surfaces.project_classes
            if "ui" in _PROJECT_CLASS_SURFACES.get(item, ())
        ]
        ui_platforms = [
            item
            for item in surfaces.target_platforms
            if "ui" in _PLATFORM_SURFACES.get(item, ())
        ]
        if ui_classes:
            reasons.append(
                "Structured project class establishes a user-facing interface: "
                + ", ".join(ui_classes)
            )
        if ui_platforms:
            reasons.append(
                "Structured target platform establishes a user-facing interface: "
                + ", ".join(ui_platforms)
            )
        if ui_proofs:
            reasons.append(
                "Plan proof contract establishes a user-facing interface: "
                + ", ".join(ui_proofs)
            )
    if lens == "performance-reliability":
        platform_surfaces = sorted(
            _PLATFORM_PERFORMANCE_SURFACES.intersection(surfaces.surfaces)
        )
        if platform_surfaces:
            reasons.append(
                "Structured platform surface establishes performance or "
                "reliability risk: "
                + ", ".join(platform_surfaces)
            )
        reliability_targets = sorted(
            _RELIABILITY_DELIVERY_TARGETS.intersection(
                surfaces.delivery_targets
            )
        )
        if reliability_targets:
            reasons.append(
                "Structured delivery contract establishes operational "
                "reliability risk: "
                + ", ".join(reliability_targets)
            )
    return tuple(reasons)


def _selection(
    role: str,
    lenses: Sequence[str],
    reasons: Sequence[str],
) -> ReviewRoleSelection:
    return ReviewRoleSelection(
        role=role,
        lenses=tuple(lenses),
        reasons=_deduplicated(list(reasons)),
    )


def legacy_roles_for_profile(profile: str) -> list[str]:
    """Return the exact v0.3 role list for compatibility Blueprints."""

    normalized = str(profile or "standard").strip().casefold()
    return list(
        LEGACY_PROFILE_ROLES.get(normalized)
        or LEGACY_PROFILE_ROLES["standard"]
    )


def select_review_policy(
    blueprint_text: str,
    tasks: Sequence[Mapping[str, Any]] = (),
    *,
    profile: str = "standard",
    source_hash: str | None = None,
) -> ReviewPolicySelection:
    """Select applicable review roles in canonical order with a hard cap of four."""

    normalized_profile = str(profile or "standard").strip().casefold()
    flags = _risk_flags(blueprint_text)
    surfaces = parse_project_surfaces(blueprint_text, tasks)
    if not flags:
        legacy_roles = legacy_roles_for_profile(normalized_profile)
        selections = tuple(
            _selection(
                role,
                (role,),
                (
                    f"Legacy Blueprint uses the `{normalized_profile}` "
                    "compatibility review profile",
                ),
            )
            for role in legacy_roles
        )
        return ReviewPolicySelection(
            legacy=True,
            profile=normalized_profile,
            source_hash=source_hash,
            project_surfaces=surfaces,
            risk_flags=(),
            selections=selections,
            combined=False,
        )

    flag_map = {flag.name: flag for flag in flags}
    logical: list[ReviewRoleSelection] = [
        _selection(
            "correctness",
            ("correctness",),
            ("Correctness review is required for every project",),
        )
    ]

    ux_reasons = (
        *_flag_reasons(flag_map, UI_FLAGS),
        *_surface_reasons(surfaces, lens="ux-accessibility"),
    )
    if ux_reasons:
        logical.append(
            _selection(
                "ux-accessibility",
                ("ux", "accessibility"),
                ux_reasons,
            )
        )

    security_reasons = (
        *_flag_reasons(flag_map, SECURITY_PRIVACY_FLAGS),
        *_surface_reasons(surfaces, lens="security"),
    )
    if security_reasons:
        logical.append(
            _selection(
                "security",
                ("security", "privacy"),
                security_reasons,
            )
        )

    architecture_reasons = _flag_reasons(flag_map, ARCHITECTURE_FLAGS)
    if architecture_reasons:
        logical.append(
            _selection(
                "architecture",
                ("architecture",),
                architecture_reasons,
            )
        )

    performance_reasons = (
        *_flag_reasons(flag_map, PERFORMANCE_RELIABILITY_FLAGS),
        *_surface_reasons(surfaces, lens="performance-reliability"),
    )
    if performance_reasons:
        logical.append(
            _selection(
                "performance-reliability",
                ("performance", "reliability"),
                performance_reasons,
            )
        )

    combined = False
    if len(logical) > MAX_REVIEW_AGENTS:
        architecture = next(
            item for item in logical if item.role == "architecture"
        )
        performance = next(
            item for item in logical if item.role == "performance-reliability"
        )
        logical = [
            item
            for item in logical
            if item.role not in {"architecture", "performance-reliability"}
        ]
        logical.append(
            _selection(
                "architecture-performance-reliability",
                ("architecture", "performance", "reliability"),
                (*architecture.reasons, *performance.reasons),
            )
        )
        combined = True

    if len(logical) > MAX_REVIEW_AGENTS:
        raise AssertionError("adaptive review policy exceeded the four-agent cap")

    return ReviewPolicySelection(
        legacy=False,
        profile=normalized_profile,
        source_hash=source_hash,
        project_surfaces=surfaces,
        risk_flags=flags,
        selections=tuple(logical),
        combined=combined,
    )


def select_review_roles(
    blueprint_text: str,
    tasks: Sequence[Mapping[str, Any]] = (),
    *,
    profile: str = "standard",
    source_hash: str | None = None,
) -> list[str]:
    """Convenience API for callers that need only stable role identifiers."""

    return list(
        select_review_policy(
            blueprint_text,
            tasks,
            profile=profile,
            source_hash=source_hash,
        ).roles
    )
