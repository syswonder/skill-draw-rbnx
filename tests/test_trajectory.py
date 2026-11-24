# SPDX-License-Identifier: MIT
"""Regression tests for the SDK outline tracer fixes."""

from __future__ import annotations

import math

import numpy as np

from xyz_draw.geometry import trajectory as trajectory_module


def test_open_contour_stops_at_its_end_instead_of_drawing_a_chord():
    """Ink clipped by the image border used to be closed by a pen-down line.

    A full-width horizontal band has an open boundary at both image edges.
    The old code appended ``poly[0]`` at z_down, drawing a straight chord
    across the whole image. No two consecutive pen-down points should now
    be farther apart than one marching-squares step.
    """
    trajectory = trajectory_module
    binary = np.zeros((12, 12), dtype=bool)
    binary[5, :] = True

    config = trajectory.PlotConfig(
        pixel_to_mm=1.0,
        offset_x=0.0,
        offset_y=0.0,
        simplify_px=0.0,
    )
    points = trajectory.to_waypoints(binary, config)

    assert points
    for (ax, ay, az), (bx, by, bz) in zip(points, points[1:]):
        if az == config.z_down and bz == config.z_down:
            assert math.hypot(bx - ax, by - ay) <= 2.0


def test_ordering_can_enter_a_contour_from_its_far_end():
    trajectory = trajectory_module
    first = [(0.0, 0.0), (10.0, 0.0)]
    closer_at_end = [(30.0, 0.0), (11.0, 0.0)]

    ordered = trajectory._order_polylines([first, closer_at_end])

    assert ordered[1][0] == (11.0, 0.0)
    assert ordered[1][-1] == (30.0, 0.0)