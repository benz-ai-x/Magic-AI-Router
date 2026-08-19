import pytest
from PIL import Image

from shellui.menu_builder import STATUS_ICON_RESOURCE, _status_color_for_connection


@pytest.mark.parametrize(
    ("status", "paused", "expected"),
    [
        ("connected", False, "green"),
        ("connecting", False, "yellow"),
        ("stopped", False, "gray"),
        ("error", False, "gray"),
        ("", False, "gray"),
        ("connected", True, "yellow"),
        ("connecting", True, "yellow"),
    ],
)
def test_connection_status_uses_three_color_language(status, paused, expected):
    assert _status_color_for_connection(status, paused) == expected


def test_menubar_icon_is_valid():
    import os
    with Image.open(os.path.join("assets", STATUS_ICON_RESOURCE)) as icon:
        assert icon.size == (256, 256)
        assert icon.mode == "RGBA"
        alpha = icon.getchannel("A")
        assert alpha.getbbox() is not None
        assert alpha.getextrema() == (0, 255)
