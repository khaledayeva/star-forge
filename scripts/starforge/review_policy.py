"""Deterministic, structured, and v0.3-compatible review-role selection."""
from __future__ import annotations
from .policy_data import mapping as _policy_mapping, value as _policy_value
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
_POLICY = _policy_value("review_policy.POLICY")
REVIEW_POLICY_SCHEMA = _POLICY["schema"]
MAX_REVIEW_AGENTS = _POLICY["max_agents"]
LEGACY_PROFILE_ROLES: dict[str, tuple[str, ...]] = _policy_value('review_policy.LEGACY_PROFILE_ROLES')
ROLE_LENSES: dict[str, str] = _policy_value('review_policy.ROLE_LENSES')
ALL_REVIEW_ROLES = frozenset(ROLE_LENSES)
RISK_FLAG_ORDER = _policy_value('review_policy.RISK_FLAG_ORDER')
UI_FLAGS = tuple(_POLICY["flag_groups"]["ui"])
SECURITY_PRIVACY_FLAGS = _policy_value('review_policy.SECURITY_PRIVACY_FLAGS')
ARCHITECTURE_FLAGS = _policy_value('review_policy.ARCHITECTURE_FLAGS')
PERFORMANCE_RELIABILITY_FLAGS = tuple(_POLICY["flag_groups"]["performance"])
_RISK_FLAG_LOOKUP = {name.casefold(): name for name in RISK_FLAG_ORDER}
_RESOLVED_FLAG_VALUES = _policy_value('review_policy._RESOLVED_FLAG_VALUES')
_UNRESOLVED_VALUES = _policy_value('review_policy._UNRESOLVED_VALUES')
_UNAVAILABLE_VALUES = _UNRESOLVED_VALUES | {"not applicable", "n/a", "na"}
_PROJECT_CLASS_SURFACES: dict[str, tuple[str, ...]] = _policy_value('review_policy._PROJECT_CLASS_SURFACES')
_PLATFORM_SURFACES: dict[str, tuple[str, ...]] = _policy_value('review_policy._PLATFORM_SURFACES')
_PROOF_SURFACES: dict[str, tuple[str, ...]] = _policy_value('review_policy._PROOF_SURFACES')
_PLATFORM_PERFORMANCE_SURFACES = frozenset(_POLICY["platform_performance_surfaces"])
_RELIABILITY_DELIVERY_TARGETS = frozenset(_POLICY["reliability_delivery_targets"])
_DELIVERY_PROOF_KINDS = frozenset(_POLICY["delivery_proof_kinds"])
_NO_DELIVERY_REVIEW_TARGETS = frozenset(_POLICY["no_delivery_review_targets"])
_DELIVERY_CONTRACT_SCHEMA = _POLICY["delivery_contract_schema"]
@dataclass(frozen=True)
class RiskFlag:
    """One normalized Risk Flags table row."""
    name: str
    value: str
    reasons: tuple[str, ...] = ()
    def to_dict(self) -> dict[str, Any]:
        return _policy_mapping("risk_flag", value=self.value, reasons=list(self.reasons))

@dataclass(frozen=True)
class ProjectSurfaces:
    """Structured project facts that may establish review applicability."""
    project_classes: tuple[str, ...] = ()
    target_platforms: tuple[str, ...] = ()
    proof_kinds: tuple[str, ...] = ()
    delivery_targets: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()
    def to_dict(self) -> dict[str, Any]:
        return _policy_mapping("project_surfaces", project_classes=list(self.project_classes), target_platforms=list(self.target_platforms), proof_kinds=list(self.proof_kinds), delivery_targets=list(self.delivery_targets), surfaces=list(self.surfaces))

@dataclass(frozen=True)
class ReviewRoleSelection:
    """One agent role, its logical lenses, and explicit applicability reasons."""
    role: str
    lenses: tuple[str, ...]
    reasons: tuple[str, ...]
    def to_dict(self) -> dict[str, Any]:
        return _policy_mapping("review_role_selection", role=self.role, lenses=list(self.lenses), reasons=list(self.reasons))

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
        item = next((item for item in self.selections if item.role == role), None)
        return item.reasons if item else ()
    def to_dict(self) -> dict[str, Any]:
        return _policy_mapping("review_policy_result", legacy=self.legacy, profile=self.profile, source_hash=self.source_hash, combined=self.combined, project_surfaces=self.project_surfaces.to_dict(), risk_flags={flag.name: flag.to_dict() for flag in self.risk_flags}, roles=list(self.roles), selections=[selection.to_dict() for selection in self.selections])
def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
def _deduplicated(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(cleaned for value in values if (cleaned := str(value or "").strip())))
def _markdown_section(text: str, title: str) -> str:
    headings = list(re.finditer(r"(?m)^\s*(?P<marks>#{1,6})\s+(?P<title>.+?)\s*#*\s*$", text))
    for index, heading in enumerate(headings):
        if heading.group("title").strip().casefold() != title.strip().casefold():
            continue
        level = len(heading.group("marks"))
        end = next((item.start() for item in headings[index + 1:]
                    if len(item.group("marks")) <= level), len(text))
        return text[heading.end():end]
    return ""
def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells: list[list[str]] = [[]]
    escaped = False
    for char in stripped:
        if escaped:
            cells[-1].append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append([])
        else:
            cells[-1].append(char)
    return ["".join(cell).strip() for cell in cells]
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
            unresolved = (raw_value.casefold() in _UNRESOLVED_VALUES
                          or bool(re.fullmatch(r"<[^>]+>", raw_value)))
            normalized_value = "unresolved" if unresolved else "invalid"
        values.setdefault(name, set()).add(normalized_value)
        if len(cells) >= 3:
            reason = _clean_cell(cells[2])
            if reason and not re.fullmatch(r"<[^>]+>", reason):
                reasons.setdefault(name, set()).add(reason)
    precedence = _POLICY["risk_precedence"]
    return tuple(
        RiskFlag(name=name, value=next(item for item in precedence if item in values[name]),
                 reasons=tuple(sorted(reasons.get(name, set()))))
        for name in RISK_FLAG_ORDER if name in values)
def _risk_contract_state(text: str, flags: Sequence[RiskFlag]) -> str:
    if not re.search(r"(?mi)^\s*#{1,6}\s+Risk Flags\s*#*\s*$", text):
        return "legacy"
    counts = {name: 0 for name in RISK_FLAG_ORDER}
    for line in _markdown_section(text, "Risk Flags").splitlines():
        cells = _table_cells(line)
        name = (_RISK_FLAG_LOOKUP.get(_clean_cell(cells[0]).casefold())
                if len(cells) >= 2 else None)
        if name is not None:
            counts[name] += 1
    complete = (all(count == 1 for count in counts.values())
                and len(flags) == len(RISK_FLAG_ORDER)
                and all(flag.value in _RESOLVED_FLAG_VALUES.values() for flag in flags))
    return "complete" if complete else "incomplete"
def _field_values(text: str, name: str) -> tuple[str, ...]:
    values: list[str] = []
    for match in re.finditer(
            rf"^\s*[-*]\s*(?:\*\*)?{re.escape(name)}(?:\*\*)?\s*:\s*(.*?)\s*$",
            text,
            re.IGNORECASE | re.MULTILINE,
    ):
        raw = _clean_cell(match.group(1))
        if not raw or re.fullmatch(r"<[^>]+>", raw) or raw.casefold() in _UNAVAILABLE_VALUES:
            continue
        for item in re.split(r"[,;/]|\s+\band\b\s+", raw, flags=re.IGNORECASE):
            normalized = _normalized(item)
            if normalized:
                values.append(normalized)
    return _deduplicated(values)
def _proof_kinds(tasks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    values: set[str] = set()
    for task in tasks:
        raw = task.get("proof")
        items = (re.split(r"[,;]", raw) if isinstance(raw, str)
                 else [str(item) for item in raw] if isinstance(raw, Sequence) else [])
        values.update(value for item in items if (value := _normalized(item)))
    return tuple(sorted(values))
def _surface_values(project_classes: Sequence[str], target_platforms: Sequence[str],
                    proof_kinds: Sequence[str]) -> tuple[str, ...]:
    sources = (
        (project_classes, _PROJECT_CLASS_SURFACES),
        (target_platforms, _PLATFORM_SURFACES),
        (proof_kinds, _PROOF_SURFACES),
    )
    return tuple(sorted({surface for values, mapping in sources
                         for value in values for surface in mapping.get(value, ())}))
def _delivery_contract_values(contract: Mapping[str, Any] | None, ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return canonical target and platform facts from a lifecycle contract."""
    if not isinstance(contract, Mapping):
        return (), ()
    if contract.get("schema") != _DELIVERY_CONTRACT_SCHEMA:
        return (), ()
    target = contract.get("target")
    if not isinstance(target, Mapping):
        return (), ()
    kind, platform = (_normalized(target.get(field)) for field in ("kind", "platform"))
    return ((kind,) if kind else ()), ((platform,) if platform else ())
def parse_project_surfaces(blueprint_text: str, tasks: Sequence[Mapping[str, Any]] = (),
                           *, delivery_contract: Mapping[str, Any] | None = None
                           ) -> ProjectSurfaces:
    """Parse only canonical Blueprint fields and Plan proof cells."""
    fields = _POLICY["field_names"]
    project_classes = _field_values(blueprint_text, fields["project_classes"])
    contract_targets, contract_platforms = _delivery_contract_values(delivery_contract)
    def combined(field: str, contract_values: Sequence[str]) -> tuple[str, ...]:
        return _deduplicated((*_field_values(blueprint_text, fields[field]),
                              *contract_values))
    target_platforms = combined("target_platforms", contract_platforms)
    delivery_targets = combined("delivery_targets", contract_targets)
    proof_kinds = _proof_kinds(tasks)
    return ProjectSurfaces(project_classes, target_platforms, proof_kinds,
                           delivery_targets,
                           _surface_values(project_classes, target_platforms, proof_kinds))
def _flag_reasons(flags: Mapping[str, RiskFlag],
                  names: Sequence[str]) -> tuple[str, ...]:
    selected = ((name, flags.get(name)) for name in names)
    return tuple(
        f"Risk flag `{name}` is yes"
        + (f": {'; '.join(flag.reasons)}" if flag.reasons else "")
        for name, flag in selected if flag is not None and flag.value == "yes")
def _surface_reasons(surfaces: ProjectSurfaces, *,
                     lens: str) -> tuple[str, ...]:
    facts: list[tuple[str, Sequence[str]]] = []
    if lens == "ux-accessibility" and "ui" in surfaces.surfaces:
        for key, values, mapping in (
            ("ui_class", surfaces.project_classes, _PROJECT_CLASS_SURFACES),
            ("ui_platform", surfaces.target_platforms, _PLATFORM_SURFACES),
            ("ui_proof", surfaces.proof_kinds, _PROOF_SURFACES),
        ):
            facts.append((key, [value for value in values
                                if "ui" in mapping.get(value, ())]))
    if lens == "performance-reliability":
        facts.extend((
            ("platform_performance", sorted(_PLATFORM_PERFORMANCE_SURFACES.intersection(surfaces.surfaces))),
            ("reliability_target", sorted(_RELIABILITY_DELIVERY_TARGETS.intersection(surfaces.delivery_targets))),
            ("delivery_target", sorted(target for target in surfaces.delivery_targets
                                       if target not in _NO_DELIVERY_REVIEW_TARGETS
                                       and target not in _RELIABILITY_DELIVERY_TARGETS)),
            ("delivery_proof", sorted(_DELIVERY_PROOF_KINDS.intersection(surfaces.proof_kinds))),
        ))
    templates = _POLICY["surface_reasons"]
    return tuple(templates[key].format(values=", ".join(values))
                 for key, values in facts if values)
def _performance_lenses(surfaces: ProjectSurfaces) -> tuple[str, ...]:
    delivery_required = any(target not in _NO_DELIVERY_REVIEW_TARGETS for target in surfaces.delivery_targets) or bool(_DELIVERY_PROOF_KINDS.intersection(surfaces.proof_kinds))
    if delivery_required:
        return ("delivery", "performance", "reliability")
    return ("performance", "reliability")
def _selection(role: str, lenses: Sequence[str],
               reasons: Sequence[str]) -> ReviewRoleSelection:
    return ReviewRoleSelection(role, tuple(lenses), _deduplicated(list(reasons)))
def _apply_agent_cap(logical: Sequence[ReviewRoleSelection], ) -> tuple[tuple[ReviewRoleSelection, ...], bool]:
    """Combine adjacent lenses when required to preserve the four-agent cap."""
    selections = list(logical)
    combined = False
    cap = _POLICY["cap"]
    if len(selections) > MAX_REVIEW_AGENTS:
        selected = {item.role: item for item in selections}
        combine_roles = cap["combine_roles"]
        if all(role in selected for role in combine_roles):
            architecture, performance = (selected[role] for role in combine_roles)
            selections = [item for item in selections if item.role not in combine_roles]
            selections.append(_selection(
                cap["combined_role"],
                _deduplicated((*cap["combined_lens_prefix"], *performance.lenses)),
                (*architecture.reasons, *performance.reasons)))
            combined = True
    if len(selections) > MAX_REVIEW_AGENTS:
        raise AssertionError(cap["error"])
    return tuple(selections), combined
def legacy_roles_for_profile(profile: str) -> list[str]:
    """Return the exact v0.3 role list for compatibility Blueprints."""
    normalized = str(profile or "standard").strip().casefold()
    return list(LEGACY_PROFILE_ROLES.get(normalized) or LEGACY_PROFILE_ROLES["standard"])
def _legacy_selections_with_surface_floors(
        profile: str, surfaces: ProjectSurfaces
) -> tuple[tuple[ReviewRoleSelection, ...], bool]:
    """Preserve legacy defaults while adding any structured surface floors."""
    legacy_reason = (_POLICY["legacy_reason"].format(profile=profile),)
    legacy = {role: _selection(role, (role, ), legacy_reason) for role in legacy_roles_for_profile(profile)}
    floors = {
        role: _surface_reasons(surfaces, lens=role)
        for role in _POLICY["legacy_floor_roles"]
    }
    if not any(floors.values()):
        return tuple(legacy.values()), False
    logical: list[ReviewRoleSelection] = []
    for role in _POLICY["legacy_role_order"]:
        if role in legacy:
            logical.append(legacy[role])
        elif floors.get(role):
            lenses = (_performance_lenses(surfaces) if role == "performance-reliability"
                      else _POLICY["legacy_floor_roles"][role])
            logical.append(_selection(role, lenses, floors[role]))
    return _apply_agent_cap(logical)
def _modern_selections(flags: Sequence[RiskFlag], surfaces: ProjectSurfaces
                       ) -> tuple[tuple[ReviewRoleSelection, ...], bool]:
    flag_map = {flag.name: flag for flag in flags}
    logical: list[ReviewRoleSelection] = []
    for descriptor in _POLICY["modern_roles"]:
        reasons = [descriptor["reason"]] if descriptor.get("always") else []
        group = descriptor.get("flag_group")
        if group:
            reasons.extend(_flag_reasons(flag_map, _POLICY["flag_groups"][group]))
        surface_lens = descriptor.get("surface_lens")
        if surface_lens:
            reasons.extend(_surface_reasons(surfaces, lens=surface_lens))
        if not reasons:
            continue
        lenses = (_performance_lenses(surfaces)
                  if descriptor.get("dynamic_lenses") == "performance"
                  else tuple(descriptor["lenses"]))
        logical.append(_selection(descriptor["role"], lenses, reasons))
    return _apply_agent_cap(logical)
def select_review_policy(blueprint_text: str, tasks: Sequence[Mapping[str, Any]] = (),
                         *, profile: str = "standard", source_hash: str | None = None,
                         delivery_contract: Mapping[str, Any] | None = None
                         ) -> ReviewPolicySelection:
    """Select applicable review roles in canonical order with a hard cap of four."""
    normalized_profile = str(profile or "standard").strip().casefold()
    flags = _risk_flags(blueprint_text)
    risk_contract_state = _risk_contract_state(blueprint_text, flags)
    surfaces = parse_project_surfaces(
        blueprint_text, tasks, delivery_contract=delivery_contract)
    legacy = risk_contract_state != "complete"
    selections, combined = (
        _modern_selections(flags, surfaces)
        if not legacy else _legacy_selections_with_surface_floors(
            normalized_profile if risk_contract_state == "legacy" else "standard",
            surfaces))
    return ReviewPolicySelection(
        legacy, normalized_profile, source_hash, surfaces, flags, selections, combined)
def select_review_roles(blueprint_text: str, tasks: Sequence[Mapping[str, Any]] = (),
                        *, profile: str = "standard", source_hash: str | None = None,
                        delivery_contract: Mapping[str, Any] | None = None) -> list[str]:
    """Convenience API for callers that need only stable role identifiers."""
    return list(select_review_policy(
        blueprint_text, tasks, profile=profile, source_hash=source_hash,
        delivery_contract=delivery_contract).roles)
