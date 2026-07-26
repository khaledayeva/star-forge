"""Small production-shaped status formatter used by the v0.4 dogfood."""

from __future__ import annotations


def format_status(phase: str, route: str, source_hash: str) -> str:
    """Return a stable operator-facing summary for one lifecycle gate."""
    normalized_phase = phase.strip().lower()
    normalized_route = route.strip().lower()
    normalized_hash = source_hash.strip().lower()
    if not normalized_phase or not normalized_route:
        raise ValueError("phase and route are required")
    if len(normalized_hash) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise ValueError("source_hash must be a lowercase SHA-256 digest")
    return (
        f"phase={normalized_phase}; route={normalized_route}; "
        f"source={normalized_hash[:12]}"
    )
