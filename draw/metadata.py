# SPDX-License-Identifier: MIT
"""PNG metadata contract shared by render_preview and image.

Generated PNGs (render_preview and prompt) are screen-upright: row 0 is
the top of the image. These text chunks let ``image`` restore the paper
coordinate system and the 1:1 millimetre scale without guessing. Third-party
PNGs without these keys keep the normal ``image`` defaults.

Pillow stores PNG ``tEXt`` chunks in ``Image.info`` as strings; the writers
in :mod:`draw.generate` and :mod:`draw.imagegen` use the same keys.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

ORIENTATION_KEY = "robonix_orientation"
PIXEL_TO_MM_KEY = "robonix_pixel_to_mm"
WIDTH_MM_KEY = "robonix_width_mm"
HEIGHT_MM_KEY = "robonix_height_mm"
OFFSET_X_KEY = "robonix_offset_x_mm"
OFFSET_Y_KEY = "robonix_offset_y_mm"


@dataclass(frozen=True)
class ImageHints:
    """Orientation / scale hints embedded in a PNG, or all defaults."""

    orientation: str = ""
    pixel_to_mm: float = 0.0
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    width_mm: float = 0.0
    height_mm: float = 0.0


def _text(info: dict, key: str) -> str:
    return str(info.get(key, "") or "").strip()


def _number(info: dict, key: str, *, positive: bool = False) -> float:
    raw = _text(info, key)
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(value):
        return 0.0
    if positive and value <= 0.0:
        return 0.0
    return value


def read_png_hints(path: str) -> ImageHints:
    """Read the Robonix orientation/scale metadata, if present.

    Missing Pillow, unreadable files, and absent keys all return defaults;
    the caller then behaves exactly like the pre-metadata ``image``.
    """
    try:
        from PIL import Image
    except ImportError:
        return ImageHints()
    try:
        with Image.open(path) as image:
            info = dict(getattr(image, "info", {}) or {})
    except (OSError, ValueError):
        return ImageHints()

    orientation = _text(info, ORIENTATION_KEY).lower()
    if orientation not in ("screen", "paper"):
        orientation = ""
    return ImageHints(
        orientation=orientation,
        pixel_to_mm=_number(info, PIXEL_TO_MM_KEY, positive=True),
        offset_x_mm=_number(info, OFFSET_X_KEY),
        offset_y_mm=_number(info, OFFSET_Y_KEY),
        width_mm=_number(info, WIDTH_MM_KEY, positive=True),
        height_mm=_number(info, HEIGHT_MM_KEY, positive=True),
    )


def resolve_flip_y(
    requested: str,
    hints: ImageHints,
    *,
    flip_y: bool = False,
) -> bool:
    """Resolve an ``orientation`` request to the vertical-mirror flag.

    ``True`` means screen orientation (the image is drawn the way it is
    displayed), which is the default for every image - including plain
    PNGs that carry no Robonix metadata. Metadata wins when present.
    ``orientation='paper'`` selects the raw raster mapping, and
    ``flip_y=true`` mirrors the default screen mapping for metadata-less
    files.
    """
    orientation = str(requested or "").strip().lower()
    if orientation in ("", "auto"):
        if hints.orientation == "screen":
            return True
        if hints.orientation == "paper":
            return False
        return not bool(flip_y)
    if orientation == "screen":
        return True
    if orientation == "paper":
        return False
    raise ValueError(
        f"unsupported orientation {orientation!r}; use 'auto', 'screen', or 'paper'"
    )