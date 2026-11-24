# SPDX-License-Identifier: MIT
"""PNG -> waypoints, backed by the vendored geometry package.

Three tracing modes are available:

``outline`` (geometry package)
    load_binary -> marching-squares ink outlines -> Douglas-Peucker.
    Correct for silhouettes and filled shapes; a drawn line becomes two
    parallel boundary sides.

``skeleton`` (this package)
    load_binary -> Zhang-Suen thinning -> skeleton graph -> polylines.
    Uses the centreline of the ink, so each line is drawn once.

``lineart`` (this package)
    difference-of-Gaussians edge extraction with a hysteresis threshold,
    then thinning -> skeleton. Turns photos / grey artwork into a line
    drawing instead of filled blobs.

Both skeleton modes join neighbouring fragments whose endpoints are within
``bridge_px`` pixels before simplification, so small raster gaps become a
short connecting stroke instead of a pen lift (dashed output).

The geometry package (``xyz_draw/geometry/{image,trajectory}.py``) still
owns loading, thresholding, paper fitting and the outline geometry.
``trace.py`` owns the skeleton/line-art algorithms.

Orientation: the geometry package maps image row 0 to the lowest y (the
paper's bottom edge), so without ``flip_y`` the drawing is vertically
mirrored relative to the PNG as displayed. Pass ``flip_y=True`` for the
screen orientation. All modes use the same placement and flip rules.

Requires numpy + Pillow (see ``requirements.txt``).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import trace as trace_mod
from .plan import PlanError, Workspace
from .geometry import image as image_module
from .geometry import trajectory as trajectory_module

TRACE_MODES = ("outline", "skeleton", "lineart")


class ImageError(Exception):
    """Unreadable image, missing dependency, empty image, or bad options."""


@dataclass(frozen=True)
class ImagePlan:
    """Waypoints plus the effective placement used to produce them."""

    points: list[tuple[float, float, float]]
    pixel_to_mm: float
    offset_x: float
    offset_y: float
    flip_x: bool
    flip_y: bool
    width_px: int
    height_px: int


def _check_mode(trace: str) -> str:
    mode = str(trace or "outline").strip().lower()
    if mode not in TRACE_MODES:
        raise ImageError(
            f"unsupported trace mode {trace!r}; use one of "
            f"{', '.join(TRACE_MODES)}"
        )
    return mode


def _skeleton_waypoints(
    binary,
    *,
    workspace: Workspace,
    pixel_to_mm: float,
    offset_x: float,
    offset_y: float,
    flip_x: bool,
    flip_y: bool,
    simplify_px: float,
    spur_px: float,
    min_length_mm: float,
    bridge_px: float,
) -> list[tuple[float, float, float]]:
    """Skeleton pixels -> pen-up/down waypoints.

    Skeleton tracing works on the centreline, so unlike the outline path
    it never returns two parallel sides for one drawn line.
    """
    skeleton = trace_mod.prune_spurs(
        trace_mod.thin(binary), max(0, int(round(float(spur_px))))
    )
    pixel_polylines = trace_mod.skeleton_polylines(skeleton)
    if not pixel_polylines:
        return []

    # (row, col) pairs; Douglas-Peucker and ordering are axis-agnostic.
    polylines = [
        [(float(row), float(col)) for row, col in poly]
        for poly in pixel_polylines
    ]
    # Merge fragments whose endpoints are only a few pixels apart before
    # simplification and min-length filtering, so short pieces join into a
    # continuous pen stroke instead of being dropped as noise.
    ordered = trace_mod.join_polylines(polylines, float(bridge_px))

    tolerance = float(simplify_px)
    minimum = float(min_length_mm)
    drawn: list[list[tuple[float, float]]] = []
    for poly in ordered:
        simple = trace_mod.simplify_polyline(poly, tolerance)
        if len(simple) < 2:
            continue
        world: list[tuple[float, float]] = []
        for row, col in simple:
            x = (col + 0.5) * pixel_to_mm + offset_x
            y = (row + 0.5) * pixel_to_mm + offset_y
            if flip_x:
                x = -x
            if flip_y:
                y = -y
            world.append((x, y))
        if minimum > 0.0 and trace_mod.polyline_length(world) < minimum:
            continue
        drawn.append(world)

    z_up = float(workspace.z_up)
    z_down = float(workspace.z_down)
    points: list[tuple[float, float, float]] = []
    for poly in drawn:
        x0, y0 = poly[0]
        points.append((x0, y0, z_up))
        points.append((x0, y0, z_down))
        for x, y in poly[1:]:
            points.append((x, y, z_down))
        last_x, last_y = poly[-1]
        points.append((last_x, last_y, z_up))
    return points


def image_to_waypoints(
    image_path: str,
    *,
    workspace: Workspace,
    scale: float = 0.0,
    offset: tuple[float, float] = (0.0, 0.0),
    threshold: float | None = None,
    invert: bool = False,
    flip_x: bool = False,
    flip_y: bool = False,
    simplify_px: float = 1.5,
    margin_mm: float = 5.0,
    fill: float = 0.8,
    trace: str = "outline",
    lineart_sigma: float = 1.2,
    lineart_detail: float = 1.0,
    spur_px: float = 2.0,
    min_length_mm: float = 0.0,
    bridge_px: float = 3.0,
) -> list[tuple[float, float, float]]:
    """Convert an image to absolute waypoints in the workspace frame.

    ``scale <= 0`` auto-fits the image to the paper, preserving aspect
    ratio, with ``margin_mm`` and ``fill``. A positive ``scale`` is the
    pixel-to-millimetre factor and uses ``offset`` as the world origin.
    ``flip_x`` / ``flip_y`` mirror the result within its bounding box.

    ``trace`` selects ``outline``, ``skeleton`` or ``lineart``; see the
    module docstring.
    """
    return image_to_waypoints_ex(
        image_path,
        workspace=workspace,
        scale=scale,
        offset=offset,
        threshold=threshold,
        invert=invert,
        flip_x=flip_x,
        flip_y=flip_y,
        simplify_px=simplify_px,
        margin_mm=margin_mm,
        fill=fill,
        trace=trace,
        lineart_sigma=lineart_sigma,
        lineart_detail=lineart_detail,
        spur_px=spur_px,
        min_length_mm=min_length_mm,
        bridge_px=bridge_px,
    ).points


def image_to_waypoints_ex(
    image_path: str,
    *,
    workspace: Workspace,
    scale: float = 0.0,
    offset: tuple[float, float] = (0.0, 0.0),
    threshold: float | None = None,
    invert: bool = False,
    flip_x: bool = False,
    flip_y: bool = False,
    simplify_px: float = 1.5,
    margin_mm: float = 5.0,
    fill: float = 0.8,
    trace: str = "outline",
    lineart_sigma: float = 1.2,
    lineart_detail: float = 1.0,
    spur_px: float = 2.0,
    min_length_mm: float = 0.0,
    bridge_px: float = 3.0,
) -> ImagePlan:
    """Like :func:`image_to_waypoints`, but also returns the placement."""
    if not image_path:
        raise ImageError("image_path is empty")
    mode = _check_mode(trace)

    try:
        workspace.validate()
    except PlanError as exc:
        raise ImageError(str(exc)) from exc

    if threshold is not None and float(threshold) <= 0.0:
        threshold = None
    try:
        if mode == "lineart":
            gray = image_module.load_gray(image_path)
            binary = trace_mod.lineart_binary(
                gray,
                sigma=float(lineart_sigma),
                detail=float(lineart_detail),
                invert=bool(invert),
            )
        else:
            binary = image_module.load_binary(
                image_path,
                threshold=None if threshold is None else float(threshold),
                invert=bool(invert),
            )
    except (OSError, ValueError) as exc:
        raise ImageError(f"cannot read image {image_path!r}: {exc}") from exc

    height_px, width_px = binary.shape
    if width_px == 0 or height_px == 0:
        raise ImageError(f"image {image_path!r} is empty")

    paper_w = workspace.x_max - workspace.x_min
    paper_h = workspace.y_max - workspace.y_min

    if scale > 0.0:
        pixel_to_mm = float(scale)
        offset_x = float(offset[0])
        offset_y = float(offset[1])
    else:
        try:
            pixel_to_mm, offset_x, offset_y, _w, _h = trajectory_module.fit_to_paper(
                width_px,
                height_px,
                paper_w_mm=paper_w,
                paper_h_mm=paper_h,
                margin_mm=float(margin_mm),
                fill=float(fill),
            )
        except ValueError as exc:
            raise ImageError(str(exc)) from exc

    if float(simplify_px) < 0.0:
        raise ImageError("simplify_px must be >= 0")

    # Mirror in place. The SDK mirrors by negating the final world
    # coordinate; pre-negating the offset and then letting the SDK apply
    # the flip keeps the result inside the same bounding box instead of
    # moving it to negative coordinates. The flip flags must be passed
    # through, otherwise only the offset would be negated.
    draw_w_mm = width_px * pixel_to_mm
    draw_h_mm = height_px * pixel_to_mm
    if flip_x:
        offset_x = -(draw_w_mm + offset_x)
    if flip_y:
        offset_y = -(draw_h_mm + offset_y)

    if mode == "outline":
        # Offsets are workspace-relative; the workspace shift is applied
        # to the finished points. It must not go through PlotConfig
        # because a flip would negate it as well.
        plot = trajectory_module.PlotConfig(
            pixel_to_mm=pixel_to_mm,
            offset_x=offset_x,
            offset_y=offset_y,
            z_up=float(workspace.z_up),
            z_down=float(workspace.z_down),
            flip_x=bool(flip_x),
            flip_y=bool(flip_y),
            simplify_px=float(simplify_px),
        )
        points = trajectory_module.to_waypoints(binary, plot)
    else:
        points = _skeleton_waypoints(
            binary,
            workspace=workspace,
            pixel_to_mm=pixel_to_mm,
            offset_x=offset_x,
            offset_y=offset_y,
            flip_x=bool(flip_x),
            flip_y=bool(flip_y),
            simplify_px=float(simplify_px),
            spur_px=float(spur_px),
            min_length_mm=float(min_length_mm),
            bridge_px=float(bridge_px),
        )

    if not points:
        if mode == "lineart":
            raise ImageError(
                "lineart produced no drawable edges; try a lower "
                "lineart detail, a different sigma, or invert=true"
            )
        if mode == "skeleton":
            raise ImageError(
                "skeleton produced no drawable lines; is the image blank? "
                "Try invert=true or trace='outline'"
            )
        raise ImageError(
            "image produced no drawable outlines; is it blank? "
            "Try invert=true for light-on-dark artwork"
        )
    if workspace.x_min or workspace.y_min:
        dx, dy = workspace.x_min, workspace.y_min
        points = [(x + dx, y + dy, z) for x, y, z in points]
    return ImagePlan(
        points=points,
        pixel_to_mm=pixel_to_mm,
        offset_x=offset_x,
        offset_y=offset_y,
        flip_x=bool(flip_x),
        flip_y=bool(flip_y),
        width_px=width_px,
        height_px=height_px,
    )