# SPDX-License-Identifier: MIT
"""Image adapter tests: raster mapping, orientation, workspace shift."""

from __future__ import annotations

import numpy as np
import pytest

from draw.image import ImageError, image_to_waypoints, image_to_waypoints_ex
from draw.plan import Workspace


def _top_left_dot(path, size: int = 40) -> str:
    """White PNG with a black square in the top-left corner."""
    arr = np.full((size, size), 255, dtype=np.uint8)
    arr[0:10, 0:10] = 0
    from PIL import Image

    Image.fromarray(arr).save(path)
    return str(path)


def _bbox(points):
    xs = [x for x, _, _ in points]
    ys = [y for _, y, _ in points]
    return min(xs), max(xs), min(ys), max(ys)


def test_default_mapping_puts_the_top_of_the_image_at_low_y(tmp_path):
    path = _top_left_dot(tmp_path / "dot.png")
    points = image_to_waypoints(path, workspace=Workspace())
    _min_x, _max_x, _min_y, max_y = _bbox(points)
    # Image row 0 is the top of the PNG; the SDK maps it to low y, so the
    # default output is vertically mirrored and stays in the lower half.
    assert max_y < 75.0


def test_flip_y_draws_the_screen_orientation(tmp_path):
    path = _top_left_dot(tmp_path / "dot.png")
    points = image_to_waypoints(path, workspace=Workspace(), flip_y=True)
    _min_x, _max_x, min_y, _max_y = _bbox(points)
    assert min_y > 75.0


def test_unlabelled_png_defaults_to_screen_orientation():
    from draw.metadata import ImageHints, resolve_flip_y

    plain = ImageHints()
    # A plain PNG (no Robonix metadata) is drawn the way it is displayed.
    assert resolve_flip_y("", plain) is True
    assert resolve_flip_y("auto", plain) is True
    # flip_y opts back into the mirrored raster mapping.
    assert resolve_flip_y("auto", plain, flip_y=True) is False
    # Explicit requests still win.
    assert resolve_flip_y("screen", plain) is True
    assert resolve_flip_y("paper", plain) is False
    # Embedded metadata wins over the default.
    assert resolve_flip_y("", ImageHints(orientation="paper")) is False
    assert resolve_flip_y("screen", ImageHints(orientation="paper")) is True


def test_unlabelled_png_default_draws_the_screen_orientation(tmp_path):
    from draw.metadata import read_png_hints, resolve_flip_y

    path = _top_left_dot(tmp_path / "dot.png")
    flip_y = resolve_flip_y("", read_png_hints(path))
    points = image_to_waypoints(path, workspace=Workspace(), flip_y=flip_y)

    # Top-left ink stays in the upper-left corner instead of mirroring.
    assert min(y for _, y, _ in points) > 75.0
    assert max(x for x, _, _ in points) < 75.0


def test_flip_x_mirrors_left_to_right(tmp_path):
    path = _top_left_dot(tmp_path / "dot.png")
    plain = image_to_waypoints(path, workspace=Workspace())
    flipped = image_to_waypoints(path, workspace=Workspace(), flip_x=True)
    assert max(x for x, _, _ in plain) < 75.0
    assert min(x for x, _, _ in flipped) > 75.0


def test_shifted_workspace_offsets_the_result(tmp_path):
    path = _top_left_dot(tmp_path / "dot.png")
    plain = image_to_waypoints(path, workspace=Workspace())
    moved = image_to_waypoints(
        path,
        workspace=Workspace(x_min=10.0, x_max=190.0, y_min=20.0, y_max=170.0),
    )
    for (px, py, pz), (mx, my, mz) in zip(plain, moved):
        assert (mx, my, mz) == pytest.approx((px + 10.0, py + 20.0, pz))


def test_missing_image_is_reported(tmp_path):
    with pytest.raises(ImageError):
        image_to_waypoints(str(tmp_path / "nope.png"), workspace=Workspace())


def test_image_plan_reports_effective_scale(tmp_path):
    path = _top_left_dot(tmp_path / "dot.png", size=40)
    plan = image_to_waypoints_ex(path, workspace=Workspace())

    assert plan.width_px == 40
    assert plan.height_px == 40
    assert plan.pixel_to_mm > 0.0
    assert plan.points == image_to_waypoints(path, workspace=Workspace())


def test_generated_metadata_draws_upright_at_one_to_one(tmp_path):
    from draw.generate import GenerateOptions, generate_scene
    from draw.metadata import read_png_hints

    path = tmp_path / "top_line.png"
    generate_scene(
        '{"width_mm": 20, "height_mm": 20, "shapes": '
        '[{"type": "line", "x1": 2, "y1": 18, "x2": 18, "y2": 18}]}',
        path,
        GenerateOptions(
            default_width_mm=20.0,
            default_height_mm=20.0,
            max_width_mm=20.0,
            max_height_mm=20.0,
            pixel_to_mm=0.1,
            line_width_mm=0.5,
            supersample=2,
            max_pixels=1000,
            max_spec_bytes=10000,
            max_strokes=10,
            max_points=1000,
        ),
    )
    hints = read_png_hints(str(path))
    assert hints.orientation == "screen"

    plan = image_to_waypoints_ex(
        str(path),
        workspace=Workspace(x_max=20.0, y_max=20.0),
        scale=hints.pixel_to_mm,
        offset=(hints.offset_x_mm, hints.offset_y_mm),
        flip_y=True,
        simplify_px=0.0,
    )

    assert plan.pixel_to_mm == pytest.approx(hints.pixel_to_mm)
    # The line is at y=18 mm (near the screen top). With the metadata
    # scale and flip_y it must stay near the top, not mirror to y=2.
    assert min(y for _, y, _ in plan.points) > 15.0


def _thick_bar(path, *, size=100, row0=45, thickness=10, col0=10, col1=90):
    """White PNG with a thick black horizontal bar."""
    arr = np.full((size, size), 255, dtype=np.uint8)
    arr[row0:row0 + thickness, col0:col1] = 0
    from PIL import Image

    Image.fromarray(arr).save(path)
    return str(path)


def _pen_down_y_spread(points, z_down):
    ys = [y for _, y, z in points if z == z_down]
    return max(ys) - min(ys)


def test_skeleton_draws_one_centreline_instead_of_two_outline_sides(tmp_path):
    path = _thick_bar(tmp_path / "bar.png")
    workspace = Workspace(x_max=100.0, y_max=100.0)

    outline = image_to_waypoints(
        path,
        workspace=workspace,
        scale=1.0,
        simplify_px=0.0,
        trace="outline",
    )
    skeleton = image_to_waypoints(
        path,
        workspace=workspace,
        scale=1.0,
        simplify_px=0.0,
        trace="skeleton",
        spur_px=2.0,
        min_length_mm=0.0,
    )

    # The outline traces the top and bottom edges of the 10 px bar.
    assert _pen_down_y_spread(outline, workspace.z_down) >= 5.0
    # The skeleton traces the middle only.
    assert _pen_down_y_spread(skeleton, workspace.z_down) <= 2.0


def test_unknown_trace_mode_is_rejected(tmp_path):
    path = _thick_bar(tmp_path / "bar.png")
    with pytest.raises(ImageError, match="trace mode"):
        image_to_waypoints(path, workspace=Workspace(), trace="magic")


def test_bridge_px_joins_nearby_fragments_into_one_stroke(tmp_path):
    arr = np.full((100, 100), 255, dtype=np.uint8)
    arr[45:55, 10:40] = 0
    arr[45:55, 43:90] = 0  # 3 px gap before thinning
    path = tmp_path / "split_bar.png"
    from PIL import Image

    Image.fromarray(arr).save(path)
    workspace = Workspace(x_max=100.0, y_max=100.0)

    def strokes(**kwargs):
        points = image_to_waypoints(
            str(path),
            workspace=workspace,
            scale=1.0,
            simplify_px=0.0,
            trace="skeleton",
            spur_px=0.0,
            min_length_mm=0.0,
            **kwargs,
        )
        return sum(1 for _, _, z in points if z == workspace.z_up)

    separate = strokes(bridge_px=0.0)
    bridged = strokes(bridge_px=8.0)
    assert separate >= 4
    assert bridged < separate