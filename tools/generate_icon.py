#!/usr/bin/env python3
"""Generate the macOS menu-bar status icons from the production app icon.

Produces three 256 px PNGs:
  - MenubarIcon.png        — full-color  (connected)
  - MenubarIcon-yellow.png — yellow      (connecting / paused)
  - MenubarIcon-gray.png   — grayscale   (disconnected)

menu_builder.py selects the appropriate one based on connection state.
"""
import numpy as np
from PIL import Image

ICON_SOURCE = "icons/magic-ai-router-macos-v2.icns"
OUTPUT_COLOR = "assets/MenubarIcon.png"
OUTPUT_YELLOW = "assets/MenubarIcon-yellow.png"
OUTPUT_GRAY = "assets/MenubarIcon-gray.png"
SIZE = 256


def _extract_square(img):
    """Crop to alpha bbox, center in a square."""
    bbox = img.getchannel("A").getbbox()
    cropped = img.crop(bbox)
    w, h = cropped.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - w) // 2, (side - h) // 2))
    return square.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    img = Image.open(ICON_SOURCE).convert("RGBA")
    color = _extract_square(img)
    color.save(OUTPUT_COLOR)
    print(f"Created {OUTPUT_COLOR} ({SIZE}x{SIZE} color)")

    arr = np.array(color).astype(np.int32)
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
    lum = (0.299 * r + 0.587 * g + 0.114 * b).clip(0, 255)
    alpha = a.astype(np.uint8)

    # Grayscale
    gray_ch = lum.astype(np.uint8)
    Image.merge("RGBA", (Image.fromarray(gray_ch), Image.fromarray(gray_ch),
                         Image.fromarray(gray_ch), Image.fromarray(alpha))
                ).save(OUTPUT_GRAY)
    print(f"Created {OUTPUT_GRAY} ({SIZE}x{SIZE} grayscale)")

    # Yellow: map luminance into warm yellow tones
    yr = lum.astype(np.uint8)
    yg = (lum * 0.78).clip(0, 255).astype(np.uint8)
    yb = (lum * 0.15).clip(0, 255).astype(np.uint8)
    Image.merge("RGBA", (Image.fromarray(yr), Image.fromarray(yg),
                         Image.fromarray(yb), Image.fromarray(alpha))
                ).save(OUTPUT_YELLOW)
    print(f"Created {OUTPUT_YELLOW} ({SIZE}x{SIZE} yellow)")


if __name__ == "__main__":
    main()
