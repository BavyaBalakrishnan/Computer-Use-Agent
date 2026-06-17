"""Set-of-marks overlay for the vision layer.

A canvas/game surface exposes no AX elements, so there is nothing to draw
numbered boxes *around* the way the Browser skill does over DOM nodes.
Instead we overlay a regular numbered GRID over the window screenshot and
ask the vision model to pick the grid cell that sits over the target. Each
mark number maps deterministically back to a window-local (x, y) pixel —
the centre of its cell — which we then click.

This converts the model's hardest task (predict exact pixels) into its
easiest (pick a numbered label), exactly as the field guide prescribes.
"""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def annotate_grid(png_bytes: bytes, *, cols: int = 8, rows: int = 6,
                  origin: tuple[int, int] = (0, 0)
                  ) -> tuple[bytes, dict[int, tuple[int, int]], float]:
    """Overlay a `cols`x`rows` numbered grid on the screenshot.

    Returns (annotated_png_bytes, mark_to_window_xy, dpr) where
    mark_to_window_xy maps each 1-based mark number to the window-LOCAL
    (x, y) centre in the same pixel space `get_window_state`/`click` use.

    `origin` is the (x, y) offset of this image's top-left within the full
    window — non-zero when the image is a CROP (two-stage zoom). Marks are
    returned already translated into full-window pixels so the caller can
    click them directly regardless of which stage produced them.

    The screenshot PNG is in device pixels; window-local click coordinates
    are also device-pixel (top-left origin of the returned PNG), so no DPR
    correction is needed here — but we surface the image's own scale for
    callers that want it.
    """
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    W, H = img.size
    ox, oy = origin
    draw = ImageDraw.Draw(img)
    cell_w, cell_h = W / cols, H / rows
    font = _font(max(14, int(min(cell_w, cell_h) * 0.22)))

    marks: dict[int, tuple[int, int]] = {}
    n = 0
    for r in range(rows):
        for c in range(cols):
            n += 1
            cx = int((c + 0.5) * cell_w)
            cy = int((r + 0.5) * cell_h)
            marks[n] = (ox + cx, oy + cy)
            # grid lines
            x0, y0 = int(c * cell_w), int(r * cell_h)
            draw.rectangle([x0, y0, int((c + 1) * cell_w), int((r + 1) * cell_h)],
                           outline=(255, 64, 64), width=1)
            # number badge: filled box + white text (readable on any bg)
            label = str(n)
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            pad = 3
            bx0, by0 = x0 + 2, y0 + 2
            draw.rectangle([bx0, by0, bx0 + tw + 2 * pad, by0 + th + 2 * pad],
                           fill=(146, 43, 33))
            draw.text((bx0 + pad, by0 + pad - tb[1]), label,
                      fill=(255, 255, 255), font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), marks, 1.0


def crop_to_cell(png_bytes: bytes, *, cols: int, rows: int, mark: int,
                 pad_frac: float = 1.0) -> tuple[bytes, tuple[int, int]]:
    """Crop the screenshot down to the cell `mark` (1-based) plus padding.

    Used by the second zoom stage: stage 1 picks a coarse cell, we crop to
    it (expanded by `pad_frac` of a cell on every side so a target straddling
    a boundary survives), then stage 2 draws a fine grid over just that crop.

    Returns (crop_png_bytes, (ox, oy)) where (ox, oy) is the crop's top-left
    offset within the full window — feed it to annotate_grid(origin=...) so
    the fine marks map back to full-window pixels.
    """
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    W, H = img.size
    cell_w, cell_h = W / cols, H / rows
    idx = mark - 1
    c, r = idx % cols, idx // cols
    px, py = pad_frac * cell_w, pad_frac * cell_h
    x0 = max(0, int(c * cell_w - px))
    y0 = max(0, int(r * cell_h - py))
    x1 = min(W, int((c + 1) * cell_w + px))
    y1 = min(H, int((r + 1) * cell_h + py))
    crop = img.crop((x0, y0, x1, y1))
    buf = BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue(), (x0, y0)


def to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def legend(marks: dict[int, tuple[int, int]]) -> str:
    """Human/LLM-readable mapping of mark → window pixel, for the prompt."""
    return "\n".join(f"  [{n}] → window pixel ({x}, {y})"
                     for n, (x, y) in sorted(marks.items()))
