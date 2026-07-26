from src.app import dashboard_payload


def test_empty_payload_is_explicit() -> None:
    assert dashboard_payload([]) == {"items": [], "empty": True}
