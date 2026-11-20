"""The application icon.

An icon is easy to ship broken in ways nobody notices until a user sees a blank
square in their taskbar: the asset can go missing, the .ico can lack small
sizes, or the spec can point at a file that is not there. These are cheap to
assert and expensive to discover late.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "packaging" / "assets"
STATIC = ROOT / "aegis" / "static"


def test_icon_assets_exist():
    for name in ("aegis.png", "aegis.ico", "aegis.icns"):
        assert (ASSETS / name).is_file(), f"packaging/assets/{name} is missing"


def test_windows_ico_has_the_sizes_explorer_asks_for():
    from PIL import Image

    with Image.open(ASSETS / "aegis.ico") as ico:
        sizes = set(ico.info.get("sizes") or [])
    # 16 is the taskbar/tray, 32 the desktop, 256 the large-tile view.
    for required in ((16, 16), (32, 32), (48, 48), (256, 256)):
        assert required in sizes, f"aegis.ico has no {required[0]}px frame"
    assert max(width for width, _ in sizes) >= 256


def test_icon_is_square_and_has_transparent_corners():
    from PIL import Image

    with Image.open(ASSETS / "aegis.png") as png:
        assert png.width == png.height >= 256
        assert png.mode == "RGBA"
        assert png.getpixel((0, 0))[3] == 0, "icon should not have an opaque box corner"


def test_runtime_copy_matches_the_source_asset():
    """The window icon and the build icon must be the same drawing."""
    assert (STATIC / "aegis.png").read_bytes() == (ASSETS / "aegis.png").read_bytes()


def test_served_page_points_at_the_icon(client):
    http, auth = client
    page = http.get("/", headers=auth).text
    assert 'rel="icon"' in page
    assert "/static/aegis.png" in page


def test_icon_is_actually_served(client):
    http, auth = client
    response = http.get("/static/aegis.png", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_spec_selects_a_per_platform_icon():
    spec = (ROOT / "packaging" / "aegis.spec").read_text(encoding="utf-8")
    for name in ("aegis.ico", "aegis.icns", "aegis.png"):
        assert name in spec, f"the spec never considers {name}"
