"""
make_icon.py — Generate StructIQ brand icons.

Produces:
  backend/icon.ico          → 256/128/64/48/32/16 px  (used by PyInstaller for the .exe)
  backend/frontend/favicon.ico → 32/16 px              (served as browser favicon)
  backend/frontend/favicon.svg → SVG                   (high-res browser favicon)

Run from the backend/ folder:
  python make_icon.py
"""
from PIL import Image, ImageDraw

# ── Brand colours ─────────────────────────────────────────────────────────────
BG         = (5, 16, 30, 255)       # #05101e  deep navy
BAR_LEFT   = (59, 130, 246, 255)    # #3b82f6
BAR_MID    = (96, 165, 250, 255)    # #60a5fa
BAR_RIGHT  = (147, 197, 253, 255)   # #93c5fd
BASE_LINE  = (59, 130, 246, 204)    # #3b82f6 @ 80 %


def draw_icon(size: int) -> Image.Image:
    """Draw the StructIQ logo at `size`×`size` pixels."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s    = size / 36  # scale relative to the 36-px SVG viewport

    # Background: rounded square
    r_bg = max(4, int(size * 0.13))
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r_bg, fill=BG)

    # Three vertical bars  (SVG coords: x, y, w, h, colour)
    bars = [
        ( 2, 14,  9, 20, BAR_LEFT),
        (14,  7,  9, 27, BAR_MID),
        (26, 18,  8, 16, BAR_RIGHT),
    ]
    r_bar = max(1, int(1.5 * s))
    for (x, y, w, h, colour) in bars:
        x1 = int(x * s)
        y1 = int(y * s)
        x2 = int((x + w) * s) - 1
        y2 = int((y + h) * s) - 1
        draw.rounded_rectangle([x1, y1, x2, y2], radius=r_bar, fill=colour)

    # Base line
    by = int(33 * s)
    bh = max(1, int(2.5 * s))
    draw.rectangle([0, by, size - 1, by + bh], fill=BASE_LINE)

    return img


def main():
    # 1. Build multi-resolution icon for the exe
    exe_sizes = [256, 128, 64, 48, 32, 16]
    frames    = [draw_icon(s) for s in exe_sizes]
    frames[0].save(
        "icon.ico",
        format   = "ICO",
        sizes    = [(s, s) for s in exe_sizes],
        append_images = frames[1:],
    )
    print("✓  icon.ico  (256 / 128 / 64 / 48 / 32 / 16 px)")

    # 2. Small favicon for the browser
    fav_sizes = [48, 32, 16]
    fav_frames = [draw_icon(s) for s in fav_sizes]
    fav_frames[0].save(
        "frontend/favicon.ico",
        format   = "ICO",
        sizes    = [(s, s) for s in fav_sizes],
        append_images = fav_frames[1:],
    )
    print("✓  frontend/favicon.ico  (48 / 32 / 16 px)")

    # 3. SVG favicon (vector — looks sharp at any size)
    svg = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">
  <rect width="36" height="36" rx="5" fill="#05101e"/>
  <rect x="2"  y="14" width="9"  height="20" rx="1.5" fill="#3b82f6"/>
  <rect x="14" y="7"  width="9"  height="27" rx="1.5" fill="#60a5fa"/>
  <rect x="26" y="18" width="8"  height="16" rx="1.5" fill="#93c5fd"/>
  <rect x="0"  y="33" width="36" height="2.5" rx="1.25" fill="#3b82f6" opacity="0.8"/>
</svg>
"""
    with open("frontend/favicon.svg", "w") as f:
        f.write(svg)
    print("✓  frontend/favicon.svg")


if __name__ == "__main__":
    main()
