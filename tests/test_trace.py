# SPDX-License-Identifier: MIT
"""Skeleton and line-art tracing tests (no Robonix runtime required)."""

from __future__ import annotations

import numpy as np

from draw import trace


def _bar(height=40, width=60, row0=18, thickness=5, col0=10, col1=50):
    arr = np.zeros((height, width), dtype=bool)
    arr[row0:row0 + thickness, col0:col1] = True
    return arr, row0, thickness


def test_thin_collapses_a_bar_to_a_single_centreline():
    bar, row0, thickness = _bar()
    skeleton = trace.thin(bar)

    assert skeleton.any()
    rows = np.nonzero(skeleton)[0]
    assert rows.min() >= row0
    assert rows.max() < row0 + thickness
    # Every interior column must keep at most one skeleton pixel: the old
    # outline strategy produced two parallel rows for the same bar.
    for col in range(12, 48):
        assert np.count_nonzero(skeleton[:, col]) <= 1


def test_prune_spurs_shortens_open_lines():
    line = np.zeros((5, 10), dtype=bool)
    line[2, :] = True

    pruned = trace.prune_spurs(line, 2)
    assert np.count_nonzero(pruned) == 6
    assert not pruned[2, 0] and not pruned[2, 1]
    assert np.count_nonzero(trace.prune_spurs(line, 0)) == 10


def test_skeleton_graph_returns_one_polyline_for_a_line():
    bar, _row0, _thickness = _bar()
    skeleton = trace.prune_spurs(trace.thin(bar), 2)
    polylines = trace.skeleton_polylines(skeleton)

    assert polylines
    longest = max(polylines, key=len)
    rows = [row for row, _col in longest]
    assert max(rows) - min(rows) <= 1
    # The walk should span most of the bar, not a stub at one end.
    cols = [col for _row, col in longest]
    assert max(cols) - min(cols) >= 30


def test_skeleton_graph_keeps_closed_loops():
    # A one-pixel diagonal diamond has no endpoints and no 8-neighbour
    # junctions: every pixel has exactly two neighbours. The graph walk
    # therefore has to start at an arbitrary pixel and come all the way
    # back to its start.
    radius, center = 10, 15
    ring = np.zeros((30, 30), dtype=bool)
    for step in range(radius + 1):
        ring[center - radius + step, center + step] = True
        ring[center + step, center + radius - step] = True
        ring[center + radius - step, center - step] = True
        ring[center - step, center - radius + step] = True

    skeleton = trace.prune_spurs(trace.thin(ring), 2)
    polylines = trace.skeleton_polylines(skeleton)

    assert polylines
    longest = max(polylines, key=len)
    assert longest[0] == longest[-1]
    assert len(longest) > 20


def test_simplify_polyline_keeps_endpoints():
    points = [(0.0, 0.0), (1.0, 0.05), (2.0, 0.0), (3.0, 0.02), (4.0, 0.0)]
    simplified = trace.simplify_polyline(points, 0.5)

    assert simplified[0] == points[0]
    assert simplified[-1] == points[-1]
    assert len(simplified) < len(points)


def test_order_polylines_uses_the_nearer_end_and_reverses():
    first = [(0.0, 0.0), (10.0, 0.0)]
    near_end = [(30.0, 0.0), (11.0, 0.0)]
    far_end = [(50.0, 0.0), (60.0, 0.0)]

    ordered = trace.order_polylines([first, near_end, far_end])
    # The second contour starts at its far end and is reversed so that it
    # begins at (11, 0), right after the first contour ends.
    assert ordered[1][0] == (11.0, 0.0)


def test_lineart_extracts_a_dark_line_from_a_light_background():
    gray = np.full((60, 80), 255.0)
    gray[:, 38:42] = 0.0

    ink = trace.lineart_binary(gray, sigma=1.2, detail=1.0)
    assert ink.any()
    cols = np.nonzero(ink)[1]
    # Hysteresis may keep a one-pixel weak halo around the hard edge.
    assert cols.min() >= 33
    assert cols.max() <= 47


def test_lineart_invert_finds_a_light_line_on_a_dark_background():
    gray = np.zeros((60, 80), dtype=float)
    gray[:, 38:42] = 255.0

    ink = trace.lineart_binary(gray, sigma=1.2, detail=1.0, invert=True)
    assert ink.any()
    cols = np.nonzero(ink)[1]
    assert cols.min() >= 33
    assert cols.max() <= 47


def test_hysteresis_keeps_connected_weak_pixels_only():
    strong = np.zeros((5, 9), dtype=bool)
    strong[2, 1] = True
    weak = strong.copy()
    weak[2, 1:8] = True
    weak[0, 8] = True  # isolated: not 8-connected to the strong pixel

    keep = trace._hysteresis(strong, weak)
    assert keep[2, 1:8].all()
    assert not keep[0, 8]


def test_join_polylines_bridges_small_gaps_only():
    left = [(0.0, 0.0), (10.0, 0.0)]
    right = [(12.0, 0.0), (20.0, 0.0)]

    joined = trace.join_polylines([left, right], 2.0)
    assert joined == [[(0.0, 0.0), (10.0, 0.0), (12.0, 0.0), (20.0, 0.0)]]
    # A gap wider than the threshold keeps the pen lift.
    assert len(trace.join_polylines([left, right], 1.0)) == 2


def test_blank_lineart_returns_nothing():
    gray = np.full((20, 20), 255.0)
    assert not trace.lineart_binary(gray, sigma=1.2, detail=1.0).any()