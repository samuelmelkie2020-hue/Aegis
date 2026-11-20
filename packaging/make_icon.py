"""The Aegis application icon, drawn as vectors so it stays crisp at any size.

    python packaging/make_icon.py

Writes packaging/assets/aegis.png (512 px, Linux window icon), aegis.ico
(Windows, multi-resolution) and aegis.icns (macOS bundle).

Why code instead of a checked-in PNG: an icon is a design asset and this one is
simple geometry, so it can be regenerated and tweaked instead of being an opaque
blob nobody can edit. The palette matches the HUD (cyan #00e5ff on #02070d).

The mark is a shield: the aegis. Engraved on it is a rotation ring and a core,
which tie it to the assistant's orb.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

CYAN = (0, 229, 255)
CYAN_DIM = (0, 160, 190)
BG = (2, 7, 13)
RING = (0, 229, 255, 120)

# Windows wants these for taskbar, alt-tab, Explorer small/large and high DPI.
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _quad(p0, p1, p2, steps: int) -> list[tuple[float, float]]:
    """Sample a quadratic Bézier, excluding its start point."""
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        points.append((
            u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
        ))
    return points


def shield_path(
    cx: float, half_width: float, top: float, bottom: float
) -> list[tuple[float, float]]:
    """A heater shield: straight shoulders, then a curve in to the bottom point.

    Clockwise from the top-left corner. Each side is one quadratic Bézier whose
    control point sits below the shoulder edge, which is what gives the shield
    its inward curl instead of a straight taper.
    """
    left, right = cx - half_width, cx + half_width
    shoulder = bottom - (bottom - top) * 0.42  # straight sides end here

    points = [(left, top), (right, top), (right, shoulder)]
    right_curve = _quad((right, shoulder), (right, bottom - (bottom - shoulder) * 0.62), (cx, bottom), 48)
    points += right_curve
    left_curve = _quad((cx, bottom), (left, bottom - (bottom - shoulder) * 0.62), (left, shoulder), 48)
    points += left_curve
    points.append((left, top))
    return points


def _background(draw_ctx, s: float) -> None:
    pad = int(s * 0.045)
    draw_ctx.rounded_rectangle(
        (pad, pad, s - pad, s - pad),
        radius=int(s * 0.22),
        fill=BG,
        outline=(*CYAN_DIM, 150),
        width=max(1, int(s * 0.012)),
    )


def _shield(draw_ctx, s: float, *, stroke: float) -> tuple[float, float, float, float]:
    cx = cy = s / 2
    half = s * 0.262
    top = cy - s * 0.30
    bottom = cy + s * 0.345
    outline = shield_path(cx, half, top, bottom)
    draw_ctx.polygon(outline, fill=(4, 26, 38, 255))
    draw_ctx.line(outline, fill=CYAN, width=max(2, int(s * stroke)), joint="curve")
    return cx, cy, top, bottom


def draw(size: int) -> Image.Image:
    """Render the mark at ``size`` px with 4x supersampling for clean edges.

    Below 32 px the dashes and ticks collapse into noise, so small sizes get a
    simplified drawing — thicker outline, larger core, nothing fine — rather
    than a downscaled version of the big one. That is how platform icon sets
    are meant to work, and it is why the 16 px tray icon stays legible.
    """
    scale = 4
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw_ctx = ImageDraw.Draw(image)
    _background(draw_ctx, s)

    small = size <= 24
    cx, cy, _top, _bottom = _shield(draw_ctx, s, stroke=0.042 if small else 0.028)

    if small:
        # One bold core, no ring, no ticks.
        core = s * 0.085
        draw_ctx.ellipse((cx - core, cy - core, cx + core, cy + core), fill=CYAN)
        return image.resize((size, size), Image.LANCZOS)

    radius = s * 0.115
    dash, gap = 22, 14
    for start_deg in range(0, 360, dash + gap):
        draw_ctx.arc(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            start=start_deg,
            end=start_deg + dash,
            fill=(*CYAN, 200),
            width=max(2, int(s * 0.016)),
        )

    core = s * 0.045
    draw_ctx.ellipse((cx - core, cy - core, cx + core, cy + core), fill=CYAN)

    tick_len, tick_off = s * 0.052, s * 0.155
    tick_w = max(2, int(s * 0.014))
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        draw_ctx.line(
            (cx + dx * tick_off, cy + dy * tick_off,
             cx + dx * (tick_off + tick_len), cy + dy * (tick_off + tick_len)),
            fill=(*CYAN, 170),
            width=tick_w,
        )

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    png = draw(512)
    png.save(ASSETS / "aegis.png")
    print(f"wrote {ASSETS / 'aegis.png'}")

    # The window icon the app loads at runtime. Same image, kept in the static
    # folder so it is resolved identically from source and inside the bundle.
    runtime_copy = HERE.parent / "aegis" / "static" / "aegis.png"
    runtime_copy.parent.mkdir(parents=True, exist_ok=True)
    png.save(runtime_copy)
    print(f"wrote {runtime_copy}")

    frames = [draw(size) for size in ICO_SIZES]
    frames[-1].save(
        ASSETS / "aegis.ico",
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=frames[:-1],
    )
    print(f"wrote {ASSETS / 'aegis.ico'} ({', '.join(str(s) for s in ICO_SIZES)})")

    # macOS .icns. Pillow writes the container; the sizes below are the ones
    # Finder, the Dock and the App Switcher actually ask for.
    icns_source = draw(1024)
    icns_source.save(ASSETS / "aegis.icns", format="ICNS")
    print(f"wrote {ASSETS / 'aegis.icns'} (from 1024 px)")


if __name__ == "__main__":
    main()
