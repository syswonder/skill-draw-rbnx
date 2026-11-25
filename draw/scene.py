# SPDX-License-Identifier: MIT
"""Declarative scene -> absolute waypoints for the ``scene`` tool.

This is the vector path that ``render_preview`` + ``image`` cannot
provide: the scene is never rasterised and no ink outline is traced. Each
stroke becomes one pen-down polyline, so a 0.5 mm requested line is drawn
once (not as two parallel outline sides) and circles/arcs keep the
geometry the caller asked for.

Coordinates are millimetres on the scene canvas (origin at the lower-left
corner, +X right, +Y up). The canvas is placed with its origin at the
workspace origin, plus an optional offset. All waypoints are validated by
:func:`draw.plan.validate_waypoints` before reaching the primitive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .generate import GenerateOptions, parse_scene
from .plan import Workspace


@dataclass(frozen=True)
class ScenePlan:
    """Pen-up/down waypoints plus scene statistics."""

    points: list[tuple[float, float, float]]
    width_mm: float
    height_mm: float
    stroke_count: int
    source_points: int


def scene_to_waypoints(
    spec: str,
    options: GenerateOptions,
    workspace: Workspace,
    *,
    offset: tuple[float, float] = (0.0, 0.0),
) -> ScenePlan:
    """Convert a validated JSON scene into absolute ``(x, y, z)`` waypoints.

    Every stroke is emitted as::

        (x0, y0, z_up)   # travel to the start
        (x0, y0, z_down) # lower the pen
        ..., (xlast, ylast, z_down)
        (xlast, ylast, z_up)

    Closed strokes repeat their first point before lifting, so the loop is
    actually closed on paper. The scene canvas origin maps to
    ``workspace.x_min/y_min`` plus ``offset``; ``validate_waypoints`` then
    rejects anything that escapes the workspace.
    """
    ox = float(offset[0])
    oy = float(offset[1])
    if not math.isfinite(ox) or not math.isfinite(oy):
        raise ValueError("offset must be finite")

    strokes, width_mm, height_mm = parse_scene(spec, options)
    dx = workspace.x_min + ox
    dy = workspace.y_min + oy

    points: list[tuple[float, float, float]] = []
    for stroke in strokes:
        pts = list(stroke.points)
        if stroke.closed and pts[0] != pts[-1]:
            pts.append(pts[0])
        if len(pts) < 2:
            continue

        x0, y0 = pts[0]
        points.append((x0 + dx, y0 + dy, workspace.z_up))
        points.append((x0 + dx, y0 + dy, workspace.z_down))
        for x, y in pts[1:]:
            points.append((x + dx, y + dy, workspace.z_down))
        last_x, last_y = pts[-1]
        points.append((last_x + dx, last_y + dy, workspace.z_up))

    return ScenePlan(
        points=points,
        width_mm=width_mm,
        height_mm=height_mm,
        stroke_count=len(strokes),
        source_points=sum(len(stroke.points) for stroke in strokes),
    )