"""Data-driven capability routing for the Star Forge control plane.

The catalog owns tool names, aliases, selectors, and fallback order. This module
only validates that contract and resolves it against capabilities discovered by
the host. It never installs a plugin or invokes a routed capability.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, make_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union
from .policy_data import mapping as _policy_mapping, value as _policy_value
ValueInput = Optional[Union[str, Iterable[str]]]
FlagInput = Optional[Union[Mapping[str, object], Iterable[str], str]]
CapabilityInput = FlagInput
ROUTING_POLICY = _policy_value("runtime_routing.POLICY")
CATALOG_SCHEMA = ROUTING_POLICY["schema"]
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / ROUTING_POLICY["catalog_path"]
SELECTOR_KEYS = tuple(ROUTING_POLICY["selector_labels"])
OPTION_KINDS = ROUTING_POLICY["option_kinds"]
class RoutingError(ValueError):
    """Raised when the routing catalog or request is inconsistent."""
def _error(name: str, **values: object) -> RoutingError:
    return RoutingError(ROUTING_POLICY["errors"][name].format(**values))

def _require(condition: object, name: str, **values: object) -> None:
    if not condition:
        raise _error(name, **values)

RouteRequest = make_dataclass(
    "RouteRequest", [(name, tuple[str, ...], field(default=()))
                     for name in ROUTING_POLICY["request_inputs"]],
    frozen=True)
RouteRequest.__module__ = __name__
RouteRequest.__doc__ = "Normalized inputs that determine which capability needs apply."

@dataclass(frozen=True)
class RouteDecision:
    """One selected capability route with explicit degradation information."""
    need: str
    purpose: str
    required_by: tuple[str, ...]
    selected: Mapping[str, Any]
    status: str
    fallback_used: bool
    unavailable: tuple[Mapping[str, Any], ...]
    install_suggestion: Mapping[str, Any] | None = None
    def to_dict(self) -> dict[str, Any]:
        values = dict(self.__dict__)
        values.update(required_by=list(self.required_by), selected=dict(self.selected),
                      unavailable=[dict(item) for item in self.unavailable],
                      install_suggestion=dict(self.install_suggestion) if self.install_suggestion else None)
        return _policy_mapping("route_decision", **values)

@dataclass(frozen=True)
class RoutingResult:
    """Deterministic routing output for all needs selected by a request."""
    schema: str
    request: RouteRequest
    decisions: tuple[RouteDecision, ...]
    @property
    def blocked(self) -> bool:
        return any(decision.status == "blocked" for decision in self.decisions)
    @property
    def degraded(self) -> bool:
        return any(decision.fallback_used for decision in self.decisions)
    def to_dict(self) -> dict[str, Any]:
        request = _policy_mapping("routing_request", **{
            field: list(value) for field, value in self.request.__dict__.items()})
        return _policy_mapping(
            "routing_result", schema=self.schema, request=request, blocked=self.blocked,
            degraded=self.degraded, decisions=[decision.to_dict() for decision in self.decisions])

def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")

def _normalized_values(values: Iterable[object] | object | None) -> tuple[str, ...]:
    source = () if values is None else (values,) if isinstance(values, str) else values
    try:
        return tuple(dict.fromkeys(item for item in map(_normalized, source) if item))
    except TypeError as exc:
        raise _error("values") from exc

def make_request(*, project_class: ValueInput = None, blueprint_flags: FlagInput = None,
                 proof_kinds: ValueInput = None, delivery_target: ValueInput = None,
                 delivery_provider: ValueInput = None,
                 required_needs: ValueInput = None,
                 material_needs: ValueInput = None) -> RouteRequest:
    """Create a normalized route request from Blueprint and Plan inputs."""
    inputs = locals()
    values = {
        field: _normalized_values(
            (key for key, enabled in inputs[name].items() if bool(enabled))
            if field == "blueprint_flags" and isinstance(inputs[name], Mapping) else inputs[name])
        for field, name in ROUTING_POLICY["request_inputs"].items()}
    return RouteRequest(**values)

def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate a capability catalog."""
    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error("load_catalog", path=catalog_path, error=exc) from exc
    validate_catalog(payload)
    return payload

def _validate_option(route_id: str, index: int, option: object, order: Mapping[str, int],
                     seen: set[str], previous_rank: int) -> int:
    _require(isinstance(option, Mapping), "option_object", route_id=route_id, index=index)
    option_id = _normalized(option.get("id"))
    _require(option_id and option_id == option.get("id"), "option_id", route_id=route_id, index=index)
    check = lambda condition, name: _require(
        condition, name, route_id=route_id, option_id=option_id)
    check(option_id not in seen, "option_duplicate")
    seen.add(option_id)
    kind = option.get("kind")
    check(kind in OPTION_KINDS, "option_kind")
    rank = order[kind]
    check(rank >= previous_rank, "option_order")
    check(str(option.get("label") or "").strip(), "option_label")
    aliases = option.get("aliases", [])
    check(isinstance(aliases, list)
          and all(isinstance(alias, str) and _normalized(alias) for alias in aliases), "option_aliases")
    install = option.get("install")
    if install is not None:
        check(kind == ROUTING_POLICY["plugin_kind"] and isinstance(install, Mapping), "install_data")
        check(str(install.get("plugin_id") or "").strip(), "install_plugin_id")
        check(install.get("requires_user_action") is True, "install_user_action")
    check(not (kind == ROUTING_POLICY["shell_kind"] and option.get("always_available")
               and option.get("safe") is not True), "unsafe_shell")
    return rank

def validate_catalog(catalog: Mapping[str, Any]) -> None:
    """Reject ambiguous or state-machine-shaped routing data."""
    _require(catalog.get("schema") == CATALOG_SCHEMA, "schema", schema=CATALOG_SCHEMA)
    order = catalog.get("preference_order")
    _require(isinstance(order, Mapping), "order_object")
    _require(set(order) == OPTION_KINDS, "order_kinds")
    _require(all(isinstance(rank, int) and rank >= 0 for rank in order.values()), "order_ranks")
    policy = catalog.get("policy")
    _require(isinstance(policy, Mapping), "policy_object")
    _require(all(policy.get(key) == value for key, value in ROUTING_POLICY["install_policy"].items()),
             "install_policy")
    routes = catalog.get("routes")
    _require(isinstance(routes, list) and routes, "routes")
    route_ids: set[str] = set()
    for index, route in enumerate(routes):
        _require(isinstance(route, Mapping), "route_object", index=index)
        route_id = _normalized(route.get("id"))
        _require(route_id and route_id == route.get("id"), "route_id", index=index)
        check = lambda condition, name, **values: _require(
            condition, name, route_id=route_id, **values)
        check(route_id not in route_ids, "route_duplicate")
        route_ids.add(route_id)
        check(str(route.get("purpose") or "").strip(), "route_purpose")
        selectors = route.get("selectors")
        check(isinstance(selectors, Mapping) and selectors, "route_selectors")
        unknown_selectors = set(selectors) - set(SELECTOR_KEYS)
        check(not unknown_selectors, "route_unknown_selectors",
              selectors=", ".join(sorted(unknown_selectors)))
        check(any(_normalized_values(value) for value in selectors.values()), "route_empty_selectors")
        options = route.get("options")
        check(isinstance(options, list) and options, "route_options")
        option_ids: set[str] = set()
        previous_rank = -1
        for option_index, option in enumerate(options):
            previous_rank = _validate_option(
                route_id, option_index, option, order, option_ids, previous_rank)

def _option_available(option: Mapping[str, Any], available: set[str]) -> bool:
    names = (option.get("id"), *option.get("aliases", []))
    return option.get("always_available") is True or bool({_normalized(name) for name in names} & available)

def _matched_selectors(route: Mapping[str, Any], request: RouteRequest) -> tuple[str, ...]:
    matches = [
        f"{label}:{value}"
        for key, label in ROUTING_POLICY["selector_labels"].items()
        for value in sorted(set(getattr(request, key))
                            & set(_normalized_values(route["selectors"].get(key))))]
    if route["id"] in request.required_needs:
        matches.append(f"required_need:{route['id']}")
    return tuple(matches)

def required_needs(catalog: Mapping[str, Any], request: RouteRequest) -> tuple[str, ...]:
    """Return route ids selected by request inputs in catalog order."""
    validate_catalog(catalog)
    known = {route["id"] for route in catalog["routes"]}
    unknown = set(request.required_needs) - known
    if unknown:
        raise _error("unknown_needs", needs=", ".join(sorted(unknown)))
    return tuple(route["id"] for route in catalog["routes"] if _matched_selectors(route, request))

def _public_option(option: Mapping[str, Any]) -> dict[str, Any]:
    result = _policy_mapping(
        "public_option", **{field: option[field] for field in ROUTING_POLICY["public_option_fields"]})
    result.update({"auth": option["auth"]} if option.get("auth") else {})
    result.update({"safe": bool(option["safe"])} if option.get("safe") is not None else {})
    return result

def _install_suggestion(route: Mapping[str, Any], request: RouteRequest,
                        unavailable: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    option = next(
        (item for item in unavailable if isinstance(item.get("install"), Mapping)), None)
    if route["id"] not in request.material_needs or option is None:
        return None
    install, policy = option["install"], ROUTING_POLICY["install_suggestion"]
    return _policy_mapping(
        "install_suggestion", need=route["id"], capability=option["id"],
        plugin_id=install["plugin_id"],
        reason=policy["reason"].format(purpose=route["purpose"], label=option["label"]),
        requires_user_action=policy["requires_user_action"], action=policy["action"])

def _route_decision(route: Mapping[str, Any], request: RouteRequest,
                    available: set[str]) -> RouteDecision:
    options = route["options"]
    selected_index = next(
        (index for index, option in enumerate(options) if _option_available(option, available)), None)
    _require(selected_index is not None, "option_unavailable", route_id=route["id"])
    selected = options[selected_index]
    unavailable = [option for option in options if not _option_available(option, available)]
    statuses = ROUTING_POLICY["statuses"]
    status = (statuses["blocked"] if selected["kind"] == ROUTING_POLICY["blocker_kind"]
              else statuses["available"] if selected_index == 0 else statuses["degraded"])
    return RouteDecision(
        need=route["id"], purpose=route["purpose"], required_by=_matched_selectors(route, request),
        selected=_public_option(selected), status=status, fallback_used=selected_index > 0,
        unavailable=tuple(_public_option(option) for option in unavailable),
        install_suggestion=(_install_suggestion(route, request, options[:selected_index])
                            if status == statuses["blocked"] else None))

def resolve_routes(*, catalog: Mapping[str, Any] | None = None,
                   catalog_path: str | Path | None = None, project_class: ValueInput = None,
                   blueprint_flags: FlagInput = None, proof_kinds: ValueInput = None,
                   delivery_target: ValueInput = None, required_needs: ValueInput = None,
                   delivery_provider: ValueInput = None,
                   material_needs: ValueInput = None,
                   available_capabilities: CapabilityInput = None) -> RoutingResult:
    """Resolve required capabilities without invoking or installing anything."""
    if catalog is not None and catalog_path is not None:
        raise _error("catalog_conflict")
    active_catalog = dict(catalog) if catalog is not None else load_catalog(catalog_path)
    validate_catalog(active_catalog)
    inputs = locals()
    request = make_request(**{
        name: inputs[name] for name in ROUTING_POLICY["request_inputs"].values()})
    route_ids = set(required_needs_for_request(active_catalog, request))
    unknown_material = set(request.material_needs) - route_ids
    if unknown_material:
        raise _error("material_conflict", needs=", ".join(sorted(unknown_material)))
    available_values = (
        (name for name, enabled in available_capabilities.items() if bool(enabled))
        if isinstance(available_capabilities, Mapping) else available_capabilities)
    available = set(_normalized_values(available_values))
    decisions = tuple(
        _route_decision(route, request, available)
        for route in active_catalog["routes"] if route["id"] in route_ids)
    return RoutingResult(schema=CATALOG_SCHEMA, request=request, decisions=decisions)
required_needs_for_request = required_needs

def resolve_route(need: str, *, available_capabilities: CapabilityInput = None,
                  material: bool = False, catalog: Mapping[str, Any] | None = None,
                  catalog_path: str | Path | None = None) -> RouteDecision:
    """Resolve one named need using the same catalog policy as a full request."""
    normalized_need = _normalized(need)
    result = resolve_routes(
        catalog=catalog,
        catalog_path=catalog_path,
        required_needs=(normalized_need, ),
        material_needs=(normalized_need, ) if material else (),
        available_capabilities=available_capabilities,
    )
    return result.decisions[0]
