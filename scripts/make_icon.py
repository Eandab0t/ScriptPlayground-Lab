"""Generate ScriptPlayground's app icon (assets/icon.ico + icon.png + static/icon.svg).

Run after changing the design below:

    python -X utf8 scripts/make_icon.py

The design is drawn with Pillow primitives at 1024px, then downscaled into a
multi-size Windows .ico (16-256 px) plus PNG and a matching vector SVG.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
STATIC = ROOT / "static"

BRAND = (88, 101, 242)      # Discord blurple
BRAND_DARK = (66, 78, 191)  # darker blurple for the gradient edge
WHITE = (255, 255, 255, 255)

SIZE = 1024
CORNER = 220  # squircle-ish rounding, matches the Discord app style


def _rounded_gradient_base() -> Image.Image:
    """Blurple rounded square with a subtle vertical two-tone gradient."""
    strip = Image.new("RGB", (1, SIZE))
    top, bottom = BRAND, BRAND_DARK
    for y in range(SIZE):
        t = y / (SIZE - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        strip.putpixel((0, y), color)
    gradient = strip.resize((SIZE, SIZE))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=CORNER, fill=255)
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base.paste(gradient, (0, 0), mask)
    return base


def draw_icon() -> Image.Image:
    """The ScriptPlayground mark: a chat bubble wrapping a code glyph `</>`.

    Swap the drawing below to redesign the icon, then re-run this script.
    """
    image = _rounded_gradient_base()
    draw = ImageDraw.Draw(image)

    # Chat bubble: white outline, tail pointing bottom-left.
    bubble_box = [128, 200, 896, 768]
    draw.rounded_rectangle(bubble_box, radius=150, outline=WHITE, width=52)
    draw.polygon([(300, 758), (300, 920), (462, 758)], fill=WHITE)

    # Code glyph </> centered in the bubble.
    stroke = 46
    left = [(332, 404), (242, 494), (332, 584)]
    right = [(692, 404), (782, 494), (692, 584)]
    for points in (left, right):
        draw.line(points, fill=WHITE, width=stroke, joint="curve")
    for point in (left[0], left[2], right[0], right[2]):
        radius = stroke // 2
        x, y = point
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=WHITE)
    draw.line([(560, 376), (464, 612)], fill=WHITE, width=stroke)

    # Three typing dots under the glyph, bottom-right.
    for index, x in enumerate((610, 680, 750)):
        r = 26 - index * 4
        draw.ellipse([x - r, 664 - r, x + r, 664 + r], fill=WHITE)

    return image


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    STATIC.mkdir(exist_ok=True)
    master = draw_icon()

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.resize((256, 256), Image.LANCZOS).save(
        ASSETS / "icon.ico", format="ICO", sizes=ico_sizes
    )
    master.resize((512, 512), Image.LANCZOS).save(ASSETS / "icon.png", format="PNG")
    master.resize((64, 64), Image.LANCZOS).save(STATIC / "icon.png", format="PNG")

    (ASSETS / "icon.svg").write_text(SVG, encoding="utf-8")
    print("wrote assets/icon.ico (16-256px), assets/icon.png, assets/icon.svg, static/icon.png")


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5865F2"/>
      <stop offset="1" stop-color="#424EBF"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="1024" rx="220" fill="url(#g)"/>
  <rect x="128" y="200" width="768" height="568" rx="150" fill="none" stroke="#fff" stroke-width="52"/>
  <path d="M300 758 L300 920 L462 758 Z" fill="#fff"/>
  <g stroke="#fff" stroke-width="46" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <path d="M332 404 L242 494 L332 584"/>
    <path d="M692 404 L782 494 L692 584"/>
    <path d="M560 376 L464 612"/>
  </g>
  <circle cx="610" cy="664" r="26" fill="#fff"/>
  <circle cx="680" cy="664" r="22" fill="#fff"/>
  <circle cx="750" cy="664" r="18" fill="#fff"/>
</svg>
"""


if __name__ == "__main__":
    main()
