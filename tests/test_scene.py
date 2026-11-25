# SPDX-License-Identifier: MIT
"""scene: declarative scene -> direct waypoints (no raster step)."""

from __future__ import annotations

import json

import pytest

from draw.generate import GenerateError, GenerateOptions
from draw.plan import PlanError, Workspace, validate_waypoints
from draw.scene import scene_to_waypoints

WS = Workspace(x_max=20.0, y_max=10.0)


def _opts(**overrides) -> GenerateOptions:
    values = dict(
        default_width_mm=20.0,
        default_height_mm=10.0,
        max_width_mm=20.0,
        max_height_mm=10.0,
        pixel_to_mm=0.25,
        line_width_mm=0.5,
        supersample=2,
        max_pixels=1000,
        max_spec_bytes=100_000,
        max_strokes=100,
        max_points=1000,
    )
    values.update(overrides)
    return GenerateOptions(**values)


def _scene(**overrides) -> dict:
    values = {
        "width_mm": 20.0,
        "height_mm": 10.0,
        "strokes": [[[1, 1], [5, 5], [9, 1]]],
    }
    values.update(overrides)
    return values


def test_single_stroke_is_one_pen_down_sequence():
    plan = scene_to_waypoints(json.dumps(_scene()), _opts(), WS)

    assert plan.stroke_count == 1
    assert plan.points == [
        (1.0, 1.0, WS.z_up),
        (1.0, 1.0, WS.z_down),
        (5.0, 5.0, WS.z_down),
        (9.0, 1.0, WS.z_down),
        (9.0, 1.0, WS.z_up),
    ]


def test_closed_shape_ends_at_its_start_without_extra_chords():
    scene = {
        "width_mm": 20.0,
        "height_mm": 10.0,
        "shapes": [{"type": "rect", "x": 1, "y": 1, "w": 3, "h": 2}],
    }
    plan = scene_to_waypoints(json.dumps(scene), _opts(), WS)

    down = [point for point in plan.points if point[2] == WS.z_down]
    assert down[0][:2] == (1.0, 1.0)
    assert down[-1][:2] == (1.0, 1.0)
    assert len(down) == 5  # start + 3 corners + returning start
    # The only pen-up point at the end is the same location, not a chord
    # back from the far corner.
    assert plan.points[-1] == (1.0, 1.0, WS.z_up)


def test_circle_is_expanded_once_and_closed():
    scene = {
        "width_mm": 20.0,
        "height_mm": 10.0,
        "shapes": [{"type": "circle", "cx": 10, "cy": 5, "r": 2}],
    }
    plan = scene_to_waypoints(json.dumps(scene), _opts(), WS)

    assert plan.source_points == 128
    down = [point for point in plan.points if point[2] == WS.z_down]
    assert len(down) == 129  # 128 sampled points + closing point
    assert down[0][:2] == down[-1][:2]


def test_workspace_origin_and_offset_shift_the_scene():
    plan = scene_to_waypoints(
        json.dumps(_scene()),
        _opts(),
        Workspace(x_min=10.0, x_max=40.0, y_min=20.0, y_max=40.0),
        offset=(2.0, 3.0),
    )
    assert plan.points[0] == (13.0, 24.0, WS.z_up)
    assert plan.points[2] == (17.0, 28.0, WS.z_down)


def test_offset_can_push_waypoints_outside_the_workspace():
    plan = scene_to_waypoints(
        json.dumps(_scene()),
        _opts(),
        WS,
        offset=(15.0, 0.0),
    )
    with pytest.raises(PlanError, match="outside workspace"):
        validate_waypoints(plan.points, workspace=WS, max_points=1000)


def test_invalid_scene_is_rejected_before_planning():
    with pytest.raises(GenerateError, match="at least one"):
        scene_to_waypoints(json.dumps({"strokes": []}), _opts(), WS)

    with pytest.raises(GenerateError, match="outside"):
        scene_to_waypoints(
            json.dumps({"strokes": [[[1, 1], [21, 2]]]}), _opts(), WS
        )


def test_offset_must_be_finite():
    with pytest.raises(ValueError, match="offset"):
        scene_to_waypoints(
            json.dumps(_scene()), _opts(), WS, offset=(float("nan"), 0.0)
        )