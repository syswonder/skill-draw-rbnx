# SPDX-License-Identifier: MIT
"""Deterministic scene -> PNG renderer for the ``render_preview`` tool.

The tool deliberately does **not** execute Python or arbitrary drawing code.
The model sends a JSON scene made of strokes and basic shapes; this module
validates every coordinate, clamps the canvas to the configured paper size,
and, when requested, renders a black-on-white PNG with the metadata that
``draw_image`` needs to reproduce the orientation and scale. Validation-only
calls (``write=False``) never import Pillow or touch the filesystem.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .metadata import (
    HEIGHT_MM_KEY,
    OFFSET_X_KEY,
    OFFSET_Y_KEY,
    ORIENTATION_KEY,
    PIXEL_TO_MM_KEY,
    WIDTH_MM_KEY,
)

MAX_LINE_WIDTH_MM = 10.0
MAX_ARC_SEGMENTS = 256


class GenerateError(ValueError):
    """The scene specification is invalid or cannot be rendered."""


@dataclass(frozen=True)
class GenerateOptions:
    """Limits and defaults for one generated image."""

    # Canvas size and limits are supplied by the caller from the primitive's
    # drawable area; there are no machine-specific defaults here.
    default_width_mm: float
    default_height_mm: float
    max_width_mm: float
    max_height_mm: float
    #: Millimetres per pixel, matching the SDK and Robonix metadata.
    pixel_to_mm: float = 0.25
    line_width_mm: float = 0.5
    supersample: int = 2
    max_pixels: int = 4096
    max_spec_bytes: int = 1_048_576
    max_strokes: int = 20_000
    max_points: int = 200_000


@dataclass(frozen=True)
class Stroke:
    points: tuple[tuple[float, float], ...]
    width_mm: float
    closed: bool = False


@dataclass(frozen=True)
class GeneratedImage:
    #: ``None`` when the scene was validated without writing a PNG.
    path: Path | None
    width_px: int
    height_px: int
    width_mm: float
    height_mm: float
    pixel_to_mm: float
    stroke_count: int
    point_count: int
    orientation: str = "screen"


def _finite(value, what: str) -> float:
    if isinstance(value, bool):
        raise GenerateError(f"{what} must be a number, got a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GenerateError(f"{what} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise GenerateError(f"{what} must be finite, got {number}")
    return number


def _positive(value, what: str) -> float:
    number = _finite(value, what)
    if number <= 0.0:
        raise GenerateError(f"{what} must be > 0, got {number}")
    return number


def _parse_points(raw, what: str, *, minimum: int = 2) -> list[tuple[float, float]]:
    if not isinstance(raw, (list, tuple)):
        raise GenerateError(f"{what} must be a list of points")
    points: list[tuple[float, float]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            if "x" not in item or "y" not in item:
                raise GenerateError(f"{what}[{index}] must contain x and y")
            x, y = item["x"], item["y"]
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = item[0], item[1]
        else:
            raise GenerateError(
                f"{what}[{index}] must be [x, y] or {{'x': ..., 'y': ...}}"
            )
        points.append((_finite(x, f"{what}[{index}].x"), _finite(y, f"{what}[{index}].y")))
    if len(points) < minimum:
        raise GenerateError(f"{what} needs at least {minimum} points")
    return points


def _number(obj: dict, key: str, what: str, default=None) -> float:
    if key not in obj or obj[key] is None:
        if default is None:
            raise GenerateError(f"{what}.{key} is required")
        return float(default)
    return _finite(obj[key], f"{what}.{key}")


def _flag(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def _width(obj: dict, what: str, default_mm: float) -> float:
    width = _positive(obj.get("width_mm", default_mm), f"{what}.width_mm")
    if width > MAX_LINE_WIDTH_MM:
        raise GenerateError(
            f"{what}.width_mm must be <= {MAX_LINE_WIDTH_MM:g}, got {width:g}"
        )
    return width


def _parse_stroke_item(item, default_width: float, index: int) -> Stroke:
    what = f"strokes[{index}]"
    if isinstance(item, (list, tuple)):
        return Stroke(
            tuple(_parse_points(item, what)), default_width, closed=False
        )
    if not isinstance(item, dict):
        raise GenerateError(f"{what} must be a point list or an object")
    points = _parse_points(item.get("points"), f"{what}.points")
    return Stroke(
        tuple(points),
        _width(item, what, default_width),
        closed=_flag(item.get("closed", False)),
    )


def _parse_shape(item, default_width: float, index: int) -> Stroke:
    what = f"shapes[{index}]"
    if not isinstance(item, dict):
        raise GenerateError(f"{what} must be an object")
    kind = str(item.get("type") or item.get("kind") or "").strip().lower()
    if not kind:
        raise GenerateError(f"{what}.type is required")

    width = _width(item, what, default_width)
    closed = False
    minimum = 2

    if kind in ("line", "segment"):
        points = [
            (_number(item, "x1", what), _number(item, "y1", what)),
            (_number(item, "x2", what), _number(item, "y2", what)),
        ]
    elif kind in ("polyline", "path"):
        points = _parse_points(item.get("points"), f"{what}.points")
        closed = _flag(item.get("closed", False))
    elif kind == "polygon":
        points = _parse_points(item.get("points"), f"{what}.points", minimum=3)
        closed = True
    elif kind in ("rect", "rectangle"):
        x = _number(item, "x", what, default=0.0)
        y = _number(item, "y", what, default=0.0)
        # `width_mm`/`height_mm` are reserved for line width metadata;
        # rectangle dimensions use w/h (or their unitless aliases).
        w = _positive(item.get("w", item.get("width")), f"{what}.w")
        h = _positive(item.get("h", item.get("height")), f"{what}.h")
        points = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        closed = True
    elif kind in ("circle", "ellipse"):
        cx = _number(item, "cx", what, default=0.0)
        cy = _number(item, "cy", what, default=0.0)
        if kind == "circle":
            rx = _positive(item.get("r", item.get("rx")), f"{what}.r")
            ry = _positive(item.get("r", item.get("ry", rx)), f"{what}.ry")
        else:
            rx = _positive(item.get("rx", item.get("r")), f"{what}.rx")
            ry = _positive(item.get("ry", item.get("r", rx)), f"{what}.ry")
        points = [
            (
                cx + rx * math.cos(2.0 * math.pi * i / 128),
                cy + ry * math.sin(2.0 * math.pi * i / 128),
            )
            for i in range(128)
        ]
        closed = True
    elif kind == "arc":
        cx = _number(item, "cx", what, default=0.0)
        cy = _number(item, "cy", what, default=0.0)
        rx = _positive(item.get("rx", item.get("r")), f"{what}.rx")
        ry = _positive(item.get("ry", item.get("r", rx)), f"{what}.ry")
        start = _number(item, "start_deg", what, default=0.0)
        end = _number(item, "end_deg", what, default=90.0)
        delta = end - start
        if abs(delta) < 1e-9:
            raise GenerateError(f"{what} start_deg and end_deg must differ")
        # Keep the requested sweep direction; never silently wrap.
        segments = max(8, min(MAX_ARC_SEGMENTS, int(abs(delta) / 5.0) + 1))
        points = [
            (
                cx + rx * math.cos(math.radians(start + delta * i / segments)),
                cy + ry * math.sin(math.radians(start + delta * i / segments)),
            )
            for i in range(segments + 1)
        ]
        minimum = 2
    else:
        raise GenerateError(f"{what}.type is unsupported: {kind!r}")

    if len(points) < minimum:
        raise GenerateError(f"{what} needs at least {minimum} points")
    return Stroke(tuple(points), width, closed=closed)


def parse_scene(
    spec: str, options: GenerateOptions
) -> tuple[list[Stroke], float, float]:
    """Parse and validate a JSON scene. Returns (strokes, width_mm, height_mm)."""
    if not isinstance(spec, str) or not spec.strip():
        raise GenerateError("spec is empty")
    if len(spec.encode("utf-8")) > options.max_spec_bytes:
        raise GenerateError(
            f"spec is too large; limit is {options.max_spec_bytes} bytes"
        )
    try:
        data = json.loads(spec)
    except json.JSONDecodeError as exc:
        raise GenerateError(f"spec is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GenerateError("spec must be a JSON object")

    width_mm = _positive(
        data.get("width_mm", options.default_width_mm), "width_mm"
    )
    height_mm = _positive(
        data.get("height_mm", options.default_height_mm), "height_mm"
    )
    max_w = _positive(options.max_width_mm, "max_width_mm")
    max_h = _positive(options.max_height_mm, "max_height_mm")
    if width_mm > max_w + 1e-9:
        raise GenerateError(
            f"width_mm must be <= {max_w:g} (paper width), got {width_mm:g}"
        )
    if height_mm > max_h + 1e-9:
        raise GenerateError(
            f"height_mm must be <= {max_h:g} (paper height), got {height_mm:g}"
        )

    default_width = _width(
        {"width_mm": options.line_width_mm}, "options", options.line_width_mm
    )
    strokes: list[Stroke] = []
    total_points = 0

    def add(stroke: Stroke) -> None:
        nonlocal total_points
        if len(strokes) >= options.max_strokes:
            raise GenerateError(f"scene has more than {options.max_strokes} strokes")
        total_points += len(stroke.points)
        if total_points > options.max_points:
            raise GenerateError(f"scene has more than {options.max_points} points")
        strokes.append(stroke)

    raw_strokes = data.get("strokes", [])
    if not isinstance(raw_strokes, list):
        raise GenerateError("strokes must be a list")
    for index, item in enumerate(raw_strokes):
        add(_parse_stroke_item(item, default_width, index))

    raw_shapes = data.get("shapes", [])
    if not isinstance(raw_shapes, list):
        raise GenerateError("shapes must be a list")
    for index, item in enumerate(raw_shapes):
        add(_parse_shape(item, default_width, index))

    if not strokes:
        raise GenerateError("scene must define at least one stroke or shape")

    for stroke_index, stroke in enumerate(strokes):
        for x, y in stroke.points:
            if x < -1e-6 or x > width_mm + 1e-6 or y < -1e-6 or y > height_mm + 1e-6:
                raise GenerateError(
                    f"stroke/shape {stroke_index} has a point ({x:g}, {y:g}) "
                    f"outside the {width_mm:g} x {height_mm:g} mm canvas"
                )
    return strokes, width_mm, height_mm


def _require_pil():
    try:
        from PIL import Image, ImageDraw, PngImagePlugin
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise GenerateError(
            "render_preview needs Pillow; install with: pip install -r requirements.txt"
        ) from exc
    return Image, ImageDraw, PngImagePlugin


def _atomic_save(image, output: Path, info) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".draw-", suffix=".png", dir=str(output.parent))
    os.close(fd)
    try:
        image.save(tmp, "PNG", pnginfo=info)
        os.replace(tmp, output)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def generate_scene(
    spec: str,
    output: Path | None,
    options: GenerateOptions,
    *,
    write: bool = True,
) -> GeneratedImage:
    """Validate ``spec`` and, when ``write`` is true, render it to ``output``.

    ``write=False`` performs every validation and geometry computation but
    never imports Pillow or touches the filesystem. The returned image then
    has ``path=None``; dimensions, stroke count, and point count are still
    populated so the caller can report what *would* be rendered.
    """
    strokes, width_mm, height_mm = parse_scene(spec, options)

    mm_per_px = _positive(options.pixel_to_mm, "pixel_to_mm")
    width_px = max(1, int(round(width_mm / mm_per_px)))
    height_px = max(1, int(round(height_mm / mm_per_px)))
    max_pixels = int(options.max_pixels)
    if max_pixels < 1:
        raise GenerateError("max_pixels must be >= 1")
    if width_px > max_pixels or height_px > max_pixels:
        raise GenerateError(
            f"PNG would be {width_px} x {height_px} px; max_pixels is {max_pixels}"
        )

    effective_mm_per_px = width_mm / width_px
    point_count = sum(len(stroke.points) for stroke in strokes)

    if not write:
        return GeneratedImage(
            path=None,
            width_px=width_px,
            height_px=height_px,
            width_mm=width_mm,
            height_mm=height_mm,
            pixel_to_mm=effective_mm_per_px,
            stroke_count=len(strokes),
            point_count=point_count,
            orientation="screen",
        )
    if output is None:
        raise GenerateError("output path is required when write=True")

    Image, ImageDraw, PngImagePlugin = _require_pil()
    supersample = max(1, min(int(options.supersample), 4))
    canvas_w = width_px * supersample
    canvas_h = height_px * supersample
    image = Image.new("L", (canvas_w, canvas_h), 255)
    draw = ImageDraw.Draw(image)
    scale_x = canvas_w / width_mm
    scale_y = canvas_h / height_mm
    px_per_mm = min(width_px / width_mm, height_px / height_mm)

    for stroke in strokes:
        points = [(x * scale_x, (height_mm - y) * scale_y) for x, y in stroke.points]
        if stroke.closed:
            points.append(points[0])
        final_width = max(1, int(round(stroke.width_mm * px_per_mm)))
        draw_width = max(1, final_width * supersample)
        kwargs = {"fill": 0, "width": draw_width}
        if draw_width > 1:
            kwargs["joint"] = "curve"
        draw.line(points, **kwargs)

    if supersample > 1:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize((width_px, height_px), resample)

    info = PngImagePlugin.PngInfo()
    info.add_text(ORIENTATION_KEY, "screen")
    info.add_text(PIXEL_TO_MM_KEY, f"{effective_mm_per_px:.12g}")
    info.add_text(WIDTH_MM_KEY, f"{width_mm:.12g}")
    info.add_text(HEIGHT_MM_KEY, f"{height_mm:.12g}")
    info.add_text(OFFSET_X_KEY, "0")
    info.add_text(OFFSET_Y_KEY, "0")

    try:
        _atomic_save(image, output, info)
    except OSError as exc:
        raise GenerateError(f"cannot write {output}: {exc}") from exc

    return GeneratedImage(
        path=output,
        width_px=width_px,
        height_px=height_px,
        width_mm=width_mm,
        height_mm=height_mm,
        pixel_to_mm=effective_mm_per_px,
        stroke_count=len(strokes),
        point_count=point_count,
        orientation="screen",
    )