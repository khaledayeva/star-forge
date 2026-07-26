"""Data-driven capability routing for the Star Forge control plane.

The catalog owns tool names, aliases, selectors, and fallback order. This module
only validates that contract and resolves it against capabilities discovered by
the host. It never installs a plugin or invokes a routed capability.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CATALOG_SCHEMA = "star-forge.capability-routing.v1"
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "capability-routing.json"
SELECTOR_KEYS = ("project_classes", "blueprint_flags", "proof_kinds", "delivery_targets")
OPTION_KINDS = {"plugin", "mcp", "native", "computer-use", "shell", "blocker"}


class RoutingError(ValueError):
    """Raised when the routing catalog or request is inconsistent."""


@dataclass(frozen=True)
class RouteRequest:
    """Normalized inputs that determine which capability needs apply."""

    project_classes: tuple[str, ...] = ()
    blueprint_flags: tuple[str, ...] = ()
    proof_kinds: tuple[str, ...] = ()
    delivery_targets: tuple[str, ...] = ()
    required_needs: tuple[str, ...] = ()
    material_needs: tuple[str, ...] = ()


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
        return {
            "need": self.need,
            "purpose": self.purpose,
            "required_by": list(self.required_by),
            "selected": dict(self.selected),
            "status": self.status,
            "fallback_used": self.fallback_used,
            "unavailable": [dict(item) for item in self.unavailable],
            "install_suggestion": (
                dict(self.install_suggestion) if self.install_suggestion else None
            ),
        }


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
        return {
            "schema": self.schema,
            "request": {
                "project_classes": list(self.request.project_classes),
                "blueprint_flags": list(self.request.blueprint_flags),
                "proof_kinds": list(self.request.proof_kinds),
                "delivery_targets": list(self.request.delivery_targets),
                "required_needs": list(self.request.required_needs),
                "material_needs": list(self.request.material_needs),
            },
            "blocked": self.blocked,
            "degraded": self.degraded,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _normalized_values(values: Iterable[object] | object | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        source: Iterable[object] = (values,)
    else:
        try:
            source = iter(values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RoutingError("routing values must be strings or iterables") from exc
    result: list[str] = []
    seen: set[str] = set()
    for value in source:
        item = _normalized(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _enabled_flags(flags: Mapping[str, object] | Iterable[object] | str | None) -> tuple[str, ...]:
    if isinstance(flags, Mapping):
        return _normalized_values(key for key, enabled in flags.items() if bool(enabled))
    return _normalized_values(flags)


def make_request(
    *,
    project_class: str | Iterable[str] | None = None,
    blueprint_flags: Mapping[str, object] | Iterable[str] | str | None = None,
    proof_kinds: str | Iterable[str] | None = None,
    delivery_target: str | Iterable[str] | None = None,
    required_needs: str | Iterable[str] | None = None,
    material_needs: str | Iterable[str] | None = None,
) -> RouteRequest:
    """Create a normalized route request from Blueprint and Plan inputs."""

    return RouteRequest(
        project_classes=_normalized_values(project_class),
        blueprint_flags=_enabled_flags(blueprint_flags),
        proof_kinds=_normalized_values(proof_kinds),
        delivery_targets=_normalized_values(delivery_target),
        required_needs=_normalized_values(required_needs),
        material_needs=_normalized_values(material_needs),
    )


def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate a capability catalog."""

    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"cannot load capability catalog {catalog_path}: {exc}") from exc
    validate_catalog(payload)
    return payload


def validate_catalog(catalog: Mapping[str, Any]) -> None:
    """Reject ambiguous or state-machine-shaped routing data."""

    if catalog.get("schema") != CATALOG_SCHEMA:
        raise RoutingError(f"catalog schema must be {CATALOG_SCHEMA}")

    order = catalog.get("preference_order")
    if not isinstance(order, Mapping):
        raise RoutingError("catalog preference_order must be an object")
    if set(order) != OPTION_KINDS:
        raise RoutingError("catalog preference_order must define every supported option kind")
    if any(not isinstance(rank, int) or rank < 0 for rank in order.values()):
        raise RoutingError("catalog preference ranks must be non-negative integers")

    policy = catalog.get("policy")
    if not isinstance(policy, Mapping):
        raise RoutingError("catalog policy must be an object")
    if policy.get("installation") != "suggest-only" or policy.get("requires_user_action") is not True:
        raise RoutingError("catalog installation policy must be suggest-only and require user action")

    routes = catalog.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RoutingError("catalog routes must be a non-empty array")

    route_ids: set[str] = set()
    for index, route in enumerate(routes):
        if not isinstance(route, Mapping):
            raise RoutingError(f"route {index} must be an object")
        route_id = _normalized(route.get("id"))
        if not route_id or route_id != route.get("id"):
            raise RoutingError(f"route {index} has an invalid id")
        if route_id in route_ids:
            raise RoutingError(f"duplicate route id: {route_id}")
        route_ids.add(route_id)
        if not str(route.get("purpose") or "").strip():
            raise RoutingError(f"route {route_id} must have a purpose")

        selectors = route.get("selectors")
        if not isinstance(selectors, Mapping) or not selectors:
            raise RoutingError(f"route {route_id} must have selectors")
        unknown_selectors = set(selectors) - set(SELECTOR_KEYS)
        if unknown_selectors:
            raise RoutingError(
                f"route {route_id} has unknown selectors: {', '.join(sorted(unknown_selectors))}"
            )
        if not any(_normalized_values(value) for value in selectors.values()):
            raise RoutingError(f"route {route_id} selectors cannot all be empty")

        options = route.get("options")
        if not isinstance(options, list) or not options:
            raise RoutingError(f"route {route_id} must have options")
        option_ids: set[str] = set()
        previous_rank = -1
        for option_index, option in enumerate(options):
            if not isinstance(option, Mapping):
                raise RoutingError(f"route {route_id} option {option_index} must be an object")
            option_id = _normalized(option.get("id"))
            if not option_id or option_id != option.get("id"):
                raise RoutingError(f"route {route_id} option {option_index} has an invalid id")
            if option_id in option_ids:
                raise RoutingError(f"route {route_id} has duplicate option id: {option_id}")
            option_ids.add(option_id)
            kind = option.get("kind")
            if kind not in OPTION_KINDS:
                raise RoutingError(f"route {route_id} option {option_id} has invalid kind")
            rank = order[kind]
            if rank < previous_rank:
                raise RoutingError(f"route {route_id} options violate preference_order")
            previous_rank = rank
            if not str(option.get("label") or "").strip():
                raise RoutingError(f"route {route_id} option {option_id} must have a label")
            aliases = option.get("aliases", [])
            if (
                not isinstance(aliases, list)
                or any(not isinstance(alias, str) or not _normalized(alias) for alias in aliases)
            ):
                raise RoutingError(f"route {route_id} option {option_id} aliases must be strings")
            install = option.get("install")
            if install is not None:
                if kind != "plugin" or not isinstance(install, Mapping):
                    raise RoutingError(f"route {route_id} option {option_id} has invalid install data")
                if not str(install.get("plugin_id") or "").strip():
                    raise RoutingError(f"route {route_id} option {option_id} install data needs plugin_id")
                if install.get("requires_user_action") is not True:
                    raise RoutingError(
                        f"route {route_id} option {option_id} install must require user action"
                    )
            if kind == "shell" and option.get("always_available") and option.get("safe") is not True:
                raise RoutingError(
                    f"route {route_id} option {option_id} cannot be an automatic unsafe shell fallback"
                )


def _availability_tokens(
    available_capabilities: Mapping[str, object] | Iterable[str] | str | None,
) -> set[str]:
    if isinstance(available_capabilities, Mapping):
        values = (name for name, available in available_capabilities.items() if bool(available))
        return set(_normalized_values(values))
    return set(_normalized_values(available_capabilities))


def _option_available(option: Mapping[str, Any], available: set[str]) -> bool:
    if option.get("always_available") is True:
        return True
    names = {_normalized(option.get("id"))}
    names.update(_normalized(alias) for alias in option.get("aliases", []))
    return bool(names & available)


def _matched_selectors(route: Mapping[str, Any], request: RouteRequest) -> tuple[str, ...]:
    request_values = {
        "project_classes": set(request.project_classes),
        "blueprint_flags": set(request.blueprint_flags),
        "proof_kinds": set(request.proof_kinds),
        "delivery_targets": set(request.delivery_targets),
    }
    matches: list[str] = []
    selectors = route["selectors"]
    labels = {
        "project_classes": "project_class",
        "blueprint_flags": "blueprint_flag",
        "proof_kinds": "proof",
        "delivery_targets": "delivery",
    }
    for key in SELECTOR_KEYS:
        requested = request_values[key]
        configured = set(_normalized_values(selectors.get(key)))
        for value in sorted(requested & configured):
            matches.append(f"{labels[key]}:{value}")
    if route["id"] in request.required_needs:
        matches.append(f"required_need:{route['id']}")
    return tuple(matches)


def required_needs(catalog: Mapping[str, Any], request: RouteRequest) -> tuple[str, ...]:
    """Return route ids selected by request inputs in catalog order."""

    validate_catalog(catalog)
    known = {route["id"] for route in catalog["routes"]}
    unknown = set(request.required_needs) - known
    if unknown:
        raise RoutingError(f"unknown required needs: {', '.join(sorted(unknown))}")
    return tuple(route["id"] for route in catalog["routes"] if _matched_selectors(route, request))


def _public_option(option: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "id": option["id"],
        "label": option["label"],
        "kind": option["kind"],
    }
    if option.get("auth"):
        result["auth"] = option["auth"]
    if option.get("safe") is not None:
        result["safe"] = bool(option["safe"])
    return result


def _install_suggestion(
    route: Mapping[str, Any],
    request: RouteRequest,
    unavailable_options: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if route["id"] not in request.material_needs:
        return None
    for option in unavailable_options:
        install = option.get("install")
        if isinstance(install, Mapping):
            return {
                "need": route["id"],
                "capability": option["id"],
                "plugin_id": install["plugin_id"],
                "reason": (
                    f"{route['purpose']} materially depends on the preferred "
                    f"{option['label']} capability."
                ),
                "requires_user_action": True,
                "action": "request-plugin-install",
            }
    return None


def resolve_routes(
    *,
    catalog: Mapping[str, Any] | None = None,
    catalog_path: str | Path | None = None,
    project_class: str | Iterable[str] | None = None,
    blueprint_flags: Mapping[str, object] | Iterable[str] | str | None = None,
    proof_kinds: str | Iterable[str] | None = None,
    delivery_target: str | Iterable[str] | None = None,
    required_needs: str | Iterable[str] | None = None,
    material_needs: str | Iterable[str] | None = None,
    available_capabilities: Mapping[str, object] | Iterable[str] | str | None = None,
) -> RoutingResult:
    """Resolve required capabilities without invoking or installing anything."""

    if catalog is not None and catalog_path is not None:
        raise RoutingError("pass catalog or catalog_path, not both")
    active_catalog = dict(catalog) if catalog is not None else load_catalog(catalog_path)
    validate_catalog(active_catalog)
    request = make_request(
        project_class=project_class,
        blueprint_flags=blueprint_flags,
        proof_kinds=proof_kinds,
        delivery_target=delivery_target,
        required_needs=required_needs,
        material_needs=material_needs,
    )
    route_ids = set(required_needs_for_request(active_catalog, request))
    unknown_material = set(request.material_needs) - route_ids
    if unknown_material:
        raise RoutingError(
            "material needs must also be required: " + ", ".join(sorted(unknown_material))
        )

    available = _availability_tokens(available_capabilities)
    decisions: list[RouteDecision] = []
    for route in active_catalog["routes"]:
        if route["id"] not in route_ids:
            continue
        options = route["options"]
        selected_index = next(
            (index for index, option in enumerate(options) if _option_available(option, available)),
            None,
        )
        if selected_index is None:
            raise RoutingError(f"route {route['id']} has no available option or explicit blocker")
        selected_option = options[selected_index]
        unavailable_options = [
            option for option in options if not _option_available(option, available)
        ]
        selected_kind = selected_option["kind"]
        status = "blocked" if selected_kind == "blocker" else (
            "available" if selected_index == 0 else "degraded"
        )
        decisions.append(
            RouteDecision(
                need=route["id"],
                purpose=route["purpose"],
                required_by=_matched_selectors(route, request),
                selected=_public_option(selected_option),
                status=status,
                fallback_used=selected_index > 0,
                unavailable=tuple(_public_option(option) for option in unavailable_options),
                install_suggestion=_install_suggestion(route, request, unavailable_options),
            )
        )
    return RoutingResult(schema=CATALOG_SCHEMA, request=request, decisions=tuple(decisions))


def required_needs_for_request(
    catalog: Mapping[str, Any],
    request: RouteRequest,
) -> tuple[str, ...]:
    """Compatibility name for callers that prefer explicit request wording."""

    return required_needs(catalog, request)


def resolve_route(
    need: str,
    *,
    available_capabilities: Mapping[str, object] | Iterable[str] | str | None = None,
    material: bool = False,
    catalog: Mapping[str, Any] | None = None,
    catalog_path: str | Path | None = None,
) -> RouteDecision:
    """Resolve one named need using the same catalog policy as a full request."""

    normalized_need = _normalized(need)
    result = resolve_routes(
        catalog=catalog,
        catalog_path=catalog_path,
        required_needs=(normalized_need,),
        material_needs=(normalized_need,) if material else (),
        available_capabilities=available_capabilities,
    )
    return result.decisions[0]


__all__ = [
    "CATALOG_SCHEMA",
    "DEFAULT_CATALOG_PATH",
    "OPTION_KINDS",
    "RouteDecision",
    "RouteRequest",
    "RoutingError",
    "RoutingResult",
    "load_catalog",
    "make_request",
    "required_needs",
    "required_needs_for_request",
    "resolve_route",
    "resolve_routes",
    "validate_catalog",
]
