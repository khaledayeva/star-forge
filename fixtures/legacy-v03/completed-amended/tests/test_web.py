from pathlib import Path


def test_dashboard_has_empty_state() -> None:
    assert "No dashboard items yet." in Path("web/index.html").read_text()
