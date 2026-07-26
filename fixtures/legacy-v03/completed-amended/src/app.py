"""Historical fixture source."""


def dashboard_payload(items: list[str]) -> dict[str, object]:
    return {"items": items, "empty": not items}
