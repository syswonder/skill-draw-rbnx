# SPDX-License-Identifier: MIT
"""Alternative raster tracing modes: skeleton and line art.

``outline`` (the SDK's marching-squares pipeline) traces the *boundary* of
the ink. That is correct for silhouettes and filled shapes, but a drawn
line becomes two parallel boundary sides. For line art we instead want the
*centreline* of the ink:

    binary -> Zhang-Suen thinning -> skeleton graph -> polylines

``lineart`` additionally converts a photograph or a grey image into a
thin edge map before skeletonising: a difference-of-Gaussians edge filter
is thresholded and thinned. It is a line-extraction pipeline, not a
tonal/halftone renderer.

Everything here is pure numpy + Pillow; no OpenCV or SciPy dependency.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

_NEIGHBORS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


class TraceError(ValueError):
    """Invalid tracing input or parameters."""


# ---------------------------------------------------------------------------
# Line art extraction
# ---------------------------------------------------------------------------
def _blur(gray: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur through Pillow (fast, C implementation)."""
    from PIL import Image, ImageFilter

    if sigma <= 0.0:
        return gray.astype(np.float64, copy=True)
    arr = np.clip(np.rint(gray), 0.0, 255.0).astype(np.uint8)
    image = Image.fromarray(arr, mode="L")
    return np.asarray(
        image.filter(ImageFilter.GaussianBlur(radius=float(sigma))),
        dtype=np.float64,
    )


def lineart_binary(
    gray: np.ndarray,
    *,
    sigma: float = 1.2,
    detail: float = 1.0,
    invert: bool = False,
) -> np.ndarray:
    """Difference-of-Gaussians edge map for photos and grey artwork.

    ``detail`` is the threshold in standard deviations above the mean
    difference. Lower values keep more edges (and more noise); higher
    values keep only strong contours. ``invert=True`` extracts light
    strokes on a dark background.

    A hysteresis threshold keeps weak edge pixels that are 8-connected to
    strong ones. A single hard threshold cuts contours wherever the local
    contrast dips, which is what makes the traced lines look dashed.
    """
    if gray.ndim != 2:
        raise TraceError("lineart needs a single-channel grey image")
    sigma = max(0.3, float(sigma))
    g1 = _blur(gray, sigma)
    g2 = _blur(gray, sigma * 1.6)
    diff = (g1 - g2) if invert else (g2 - g1)

    mean = float(diff.mean())
    std = float(diff.std())
    high = mean + max(0.0, float(detail)) * std
    if not math.isfinite(high) or high <= 0.0:
        high = float(np.percentile(diff, 95.0))
    high = max(1.0, high)

    # Weak edges only survive when connected to a strong one; this keeps
    # contours continuous without admitting isolated sensor noise.
    low = mean + max(0.0, float(detail)) * 0.45 * std
    if not math.isfinite(low) or low <= 0.0:
        low = high * 0.5
    low = min(max(1.0, low), high)

    strong = diff > high
    if not strong.any():
        # A faint but real edge should still produce something; fall back
        # to the strongest 1% and keep its weaker neighbours.
        high = max(1.0, float(np.percentile(diff, 99.0)))
        low = max(1.0, min(float(np.percentile(diff, 95.0)), high))
        strong = diff > high
        if not strong.any():
            return np.zeros(diff.shape, dtype=bool)
    weak = diff > low
    return _hysteresis(strong, weak)


def _hysteresis(strong: np.ndarray, weak: np.ndarray) -> np.ndarray:
    """Grow *strong* through 8-connected *weak* pixels."""
    keep = np.asarray(strong, dtype=bool).copy()
    weak = np.asarray(weak, dtype=bool)
    height, width = weak.shape
    queue = deque(
        (int(row), int(col)) for row, col in zip(*np.nonzero(keep))
    )
    while queue:
        row, col = queue.popleft()
        r0, r1 = max(0, row - 1), min(height, row + 2)
        c0, c1 = max(0, col - 1), min(width, col + 2)
        block = weak[r0:r1, c0:c1] & ~keep[r0:r1, c0:c1]
        if not block.any():
            continue
        keep[r0:r1, c0:c1] |= block
        queue.extend(
            (r0 + int(r), c0 + int(c)) for r, c in zip(*np.nonzero(block))
        )
    return keep


# ---------------------------------------------------------------------------
# Zhang-Suen thinning
# ---------------------------------------------------------------------------
def _transitions(p2, p3, p4, p5, p6, p7, p8, p9) -> np.ndarray:
    """Count 0->1 transitions around the 8-neighbour ring."""
    a = np.zeros_like(p2, dtype=np.uint8)
    for prev, nxt in (
        (p2, p3), (p3, p4), (p4, p5), (p5, p6),
        (p6, p7), (p7, p8), (p8, p9), (p9, p2),
    ):
        a += ((prev == 0) & (nxt == 1)).astype(np.uint8)
    return a


def thin(binary: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning: reduce ink regions to a 1-pixel centreline.

    The input is padded so images whose ink touches the border are
    processed instead of raising or wrapping. The operation is iterative;
    thick filled regions are deliberately slow because every erosion pass
    is vectorised over the whole image.
    """
    img = np.asarray(binary, dtype=bool)
    if img.ndim != 2:
        raise TraceError("thinning needs a 2-D binary image")
    if not img.any():
        return img.copy()

    padded = np.pad(img.astype(np.uint8), 1)
    while True:
        changed = False
        for first_step in (True, False):
            p = padded
            p2 = p[:-2, 1:-1]  # north
            p3 = p[:-2, 2:]    # north-east
            p4 = p[1:-1, 2:]   # east
            p5 = p[2:, 2:]     # south-east
            p6 = p[2:, 1:-1]   # south
            p7 = p[2:, :-2]    # south-west
            p8 = p[1:-1, :-2]  # west
            p9 = p[:-2, :-2]   # north-west

            count = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            transitions = _transitions(p2, p3, p4, p5, p6, p7, p8, p9)
            if first_step:
                m1 = (p2 * p4 * p6) == 0
                m2 = (p4 * p6 * p8) == 0
            else:
                m1 = (p2 * p4 * p8) == 0
                m2 = (p2 * p6 * p8) == 0

            remove = (
                (p[1:-1, 1:-1] == 1)
                & (count >= 2) & (count <= 6)
                & (transitions == 1)
                & m1 & m2
            )
            if remove.any():
                p[1:-1, 1:-1][remove] = 0
                changed = True
        if not changed:
            break
    return padded[1:-1, 1:-1].astype(bool)


def _neighbour_count(binary: np.ndarray) -> np.ndarray:
    h, w = binary.shape
    padded = np.pad(binary.astype(np.uint8), 1)
    count = np.zeros((h, w), dtype=np.uint8)
    for dr, dc in _NEIGHBORS:
        count += padded[1 + dr:1 + dr + h, 1 + dc:1 + dc + w]
    return count


def prune_spurs(binary: np.ndarray, iterations: int) -> np.ndarray:
    """Peel short skeleton branches by removing endpoint pixels.

    Each pass removes every pixel with exactly one 8-neighbour. Running
    ``iterations`` passes removes spurs shorter than that; it also trims
    the tip of every legitimate open line by the same amount, which is
    typically 1-3 pixels and below the pen width.
    """
    out = np.asarray(binary, dtype=bool).copy()
    if iterations <= 0:
        return out
    for _ in range(int(iterations)):
        endpoints = out & (_neighbour_count(out) == 1)
        if not endpoints.any():
            break
        out[endpoints] = False
    return out


# ---------------------------------------------------------------------------
# Skeleton graph -> polylines
# ---------------------------------------------------------------------------
def _neighbours(point, pixels):
    row, col = point
    return [
        (row + dr, col + dc)
        for dr, dc in _NEIGHBORS
        if (row + dr, col + dc) in pixels
    ]


def skeleton_polylines(skeleton: np.ndarray) -> list[list[tuple[int, int]]]:
    """Split a 1-pixel skeleton into open polylines and closed loops.

    Pixels with degree != 2 are junctions/endpoints and start a walk;
    remaining unvisited pixels belong to loops with no junction. A walk
    stops at the next junction, an endpoint, or back at its start.
    """
    skel = np.asarray(skeleton, dtype=bool)
    if not skel.any():
        return []
    pixels = {
        (int(row), int(col))
        for row, col in zip(*np.nonzero(skel))
    }
    if not pixels:
        return []
    degree = {point: len(_neighbours(point, pixels)) for point in pixels}
    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    polylines: list[list[tuple[int, int]]] = []

    def walk(start, first):
        poly = [start, first]
        visited.add((start, first))
        visited.add((first, start))
        previous, current = start, first
        while degree.get(current, 0) == 2:
            step = None
            for candidate in _neighbours(current, pixels):
                if candidate != previous and (current, candidate) not in visited:
                    step = candidate
                    break
            if step is None:
                break
            visited.add((current, step))
            visited.add((step, current))
            poly.append(step)
            previous, current = current, step
            if current == start:
                break
        return poly

    # Branches start at endpoints (degree 1) and junctions (degree >= 3).
    for point in pixels:
        if degree[point] == 2:
            continue
        for neighbour in _neighbours(point, pixels):
            if (point, neighbour) not in visited:
                polylines.append(walk(point, neighbour))

    # Pure loops have no junction pixel, so nothing above started them.
    for point in pixels:
        if degree[point] != 2:
            continue
        for neighbour in _neighbours(point, pixels):
            if (point, neighbour) not in visited:
                polylines.append(walk(point, neighbour))

    return [poly for poly in polylines if len(poly) >= 2]


# ---------------------------------------------------------------------------
# Polyline simplification and ordering
# ---------------------------------------------------------------------------
def simplify_polyline(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    """Douglas-Peucker simplification (iterative, no recursion limit)."""
    n = len(points)
    if n < 3 or tolerance <= 0.0:
        return list(points)
    tol2 = float(tolerance) ** 2
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i]
        bx, by = points[j]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        far, far_d = -1, -1.0
        for k in range(i + 1, j):
            px, py = points[k]
            if seg2 == 0.0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                d2 = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2
            if d2 > far_d:
                far_d, far = d2, k
        if far_d > tol2:
            keep[far] = True
            stack.append((i, far))
            stack.append((far, j))
    return [point for point, kept in zip(points, keep) if kept]


def _dist2(a, b) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def order_polylines(
    polylines: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Greedy nearest-neighbour ordering, allowing endpoint reversal."""
    remaining = [list(poly) for poly in polylines if poly]
    if not remaining:
        return []
    ordered = [remaining.pop(0)]
    while remaining:
        end = ordered[-1][-1]
        best_index = 0
        best_reverse = False
        best_distance = _dist2(end, remaining[0][0])
        for index, poly in enumerate(remaining):
            d_start = _dist2(end, poly[0])
            d_end = _dist2(end, poly[-1])
            if d_start <= d_end:
                distance, reverse = d_start, False
            else:
                distance, reverse = d_end, True
            if distance < best_distance:
                best_index, best_reverse, best_distance = index, reverse, distance
        current = remaining.pop(best_index)
        if best_reverse:
            current.reverse()
        ordered.append(current)
    return ordered


def join_polylines(
    polylines: list[list[tuple[float, float]]],
    max_gap: float,
) -> list[list[tuple[float, float]]]:
    """Order polylines, then merge chains whose endpoints are close.

    Thinning can split one visual line into several polylines at tiny gaps
    or weak edge pixels. Drawing them separately makes the pen lift and
    produces a dashed result. When the nearest endpoint of the next
    polyline is within *max_gap* (same units as the points), append it to
    the previous chain: the connecting segment is a short straight pen
    stroke instead of a gap.
    """
    ordered = order_polylines(polylines)
    if max_gap <= 0.0 or not ordered:
        return ordered
    gap2 = float(max_gap) ** 2
    merged: list[list[tuple[float, float]]] = [list(ordered[0])]
    for poly in ordered[1:]:
        if not poly:
            continue
        if _dist2(merged[-1][-1], poly[0]) <= gap2:
            merged[-1].extend(poly)
        else:
            merged.append(list(poly))
    return merged


def polyline_length(points: list[tuple[float, float]]) -> float:
    """Total sketched length in the points' units."""
    return sum(
        math.hypot(bx - ax, by - ay)
        for (ax, ay), (bx, by) in zip(points, points[1:])
    )