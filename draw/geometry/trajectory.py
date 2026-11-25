"""二值图 -> XYZ 绘图路径点（waypoints）。

路径点格式：``[(x_mm, y_mm, z_mm), ...]``，z 值直接编码笔状态：
``z_down`` 落笔、``z_up`` 抬笔。PLC 无需区分提笔/落笔，逐点执行
``MC_MoveLinearAbsolute`` 即可。

策略：marching-squares 提取墨迹**边界轮廓**，Douglas-Peucker 抽稀后按最近邻顺序落笔。
（原 raster 逐行扫描方案已移除：线稿会被切成大量短游程，抬落笔次数过多、极慢。）

单位约定（与整机一致）：
    - 轨迹坐标全部是**毫米**，原点在纸面左下角，+X 向右、+Y 向上；
    - z 直接编码笔状态：z_down（落笔）、z_up（抬笔），由调用方从
      plotter primitive 传入；
    - 上机对接 Robonix 时，标准机械臂契约用**米**，换算在
      primitive 边界做（x_mm / 1000.0），本模块内部始终是毫米。

轮廓 = 墨迹的外边界，所以画一条有宽度的线会得到"两侧两条线"（描边效果）；
要单线笔画请用 text 模块（Hershey / 汉字中线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class PlotConfig:
    pixel_to_mm: float = 0.1          # 1 像素对应的世界坐标距离 (mm)
    offset_x: float = 0.0             # 世界坐标原点偏移 (mm)
    offset_y: float = 0.0
    z_up: float = 0.0                # 抬笔高度 (mm)：由调用方从 primitive 传入
    z_down: float = 0.0              # 落笔高度 (mm)：压在纸上
    vel: float = 20.0                 # 落笔运动速度
    travel_vel: float = 40.0          # 抬笔移动速度
    acc: float = 300.0
    dec: float = 300.0
    jerk: float = 3000.0
    flip_x: bool = False
    flip_y: bool = False              # 图像 row 默认映射到 +y
    # 轮廓抽稀容差（像素）：Douglas-Peucker，0 = 不抽稀
    simplify_px: float = 1.5


def _pixel_to_world(col: float, row: float, cfg: PlotConfig) -> tuple[float, float]:
    x = col * cfg.pixel_to_mm + cfg.offset_x
    y = row * cfg.pixel_to_mm + cfg.offset_y
    if cfg.flip_x:
        x = -x
    if cfg.flip_y:
        y = -y
    return x, y


def _pt(col: int, row: int, z: float, cfg: PlotConfig) -> tuple[float, float, float]:
    x, y = _pixel_to_world(col + 0.5, row + 0.5, cfg)
    return x, y, z


# ---------------------------------------------------------------------------
# outline 策略（marching squares）
# ---------------------------------------------------------------------------
# 边编号: 0=下边中点 1=右边中点 2=上边中点 3=左边中点
# 角点编号: bit0=左上 bit1=右上 bit2=右下 bit3=左下
_EDGE_TABLE = [
    [],                        # 0  全在外
    [(3, 2)],                  # 1  左上
    [(2, 1)],                  # 2  右上
    [(3, 1)],                  # 3  上两格
    [(1, 0)],                  # 4  右下
    [(3, 2), (1, 0)],          # 5  鞍点
    [(2, 0)],                  # 6  右两格
    [(3, 0)],                  # 7  左下在外
    [(0, 3)],                  # 8  左下
    [(2, 0)],                  # 9  左两格
    [(2, 1), (0, 3)],          # 10 鞍点
    [(0, 1)],                  # 11 右下在外
    [(1, 3)],                  # 12 下两格
    [(1, 2)],                  # 13 右上在外
    [(2, 3)],                  # 14 左上在外
    [],                        # 15 全在内
]


def _vertex(edge: int, x0: float, y0: float) -> tuple[float, float]:
    # 行号向下增大：「下边」在 y0+1、「上边」在 y0
    # 边 0: 下, 1: 右, 2: 上, 3: 左
    if edge == 0:
        return x0 + 0.5, y0 + 1.0
    if edge == 1:
        return x0 + 1.0, y0 + 0.5
    if edge == 2:
        return x0 + 0.5, y0
    return x0, y0 + 0.5


def _marching_squares_segments(binary: np.ndarray) -> list[list[tuple[float, float]]]:
    """对二值图做 marching squares，返回所有轮廓线段（每条线段两顶点）。"""
    rows, cols = binary.shape
    segments: list[list[tuple[float, float]]] = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            idx = 0
            if binary[r, c]:
                idx |= 1
            if binary[r, c + 1]:
                idx |= 2
            if binary[r + 1, c + 1]:
                idx |= 4
            if binary[r + 1, c]:
                idx |= 8
            for e0, e1 in _EDGE_TABLE[idx]:
                segments.append([_vertex(e0, c, r), _vertex(e1, c, r)])
    return segments


def _segments_to_polylines(
    segments: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """把无序线段按端点相接串成折线（端点哈希索引，O(n)）。"""
    def key(p):
        return (round(float(p[0]), 6), round(float(p[1]), 6))

    adj: dict = {}
    for i, (a, b) in enumerate(segments):
        ka, kb = key(a), key(b)
        adj.setdefault(ka, []).append((i, kb))
        adj.setdefault(kb, []).append((i, ka))

    used = [False] * len(segments)
    polylines: list[list[tuple[float, float]]] = []
    for i0 in range(len(segments)):
        if used[i0]:
            continue
        used[i0] = True
        poly = [key(segments[i0][0]), key(segments[i0][1])]
        for at_tail in (True, False):
            while True:
                cur = poly[-1] if at_tail else poly[0]
                nxt = None
                for j, other in adj.get(cur, ()):
                    if not used[j]:
                        used[j] = True
                        nxt = other
                        break
                if nxt is None:
                    break
                if at_tail:
                    poly.append(nxt)
                else:
                    poly.insert(0, nxt)
        polylines.append(poly)
    return polylines


def _close(a: tuple[float, float], b: tuple[float, float], eps: float) -> bool:
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def _rdp(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker 折线抽稀（迭代实现，避免深递归）。"""
    n = len(points)
    if n < 3 or tol <= 0:
        return points
    tol2 = tol * tol
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
    return [p for p, k in zip(points, keep) if k]


def _simplify_loop(poly: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """闭合折线抽稀；tol <= 0 时原样返回。"""
    if tol <= 0 or len(poly) <= 4:
        return poly
    closed = _close(poly[0], poly[-1], 1e-9)
    body = poly[:-1] if closed else poly
    simple = _rdp(body, tol)
    if closed and simple:
        simple = simple + [simple[0]]
    return simple


def _order_polylines(
    world: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Greedy nearest-neighbour ordering that may reverse a polyline.

    Comparing only the previous endpoint with the *start* of every
    remaining polyline (the old behaviour) can pick a contour whose end is
    much closer, and it can never traverse a contour in the cheaper
    direction. Both endpoints are considered here and a contour is
    reversed when its end is the nearer entry point. This reduces pen-up
    travel and, more importantly, avoids dragging the pen across the
    drawing when the lift is imperfect.
    """
    if not world:
        return []
    ordered: list[list[tuple[float, float]]] = []
    current = world.pop(0)
    ordered.append(current)
    while world:
        end = current[-1]
        best_index = 0
        best_distance = _dist2(end, world[0][0])
        best_reverse = False
        for index, poly in enumerate(world):
            d_start = _dist2(end, poly[0])
            d_end = _dist2(end, poly[-1])
            if d_start <= d_end:
                distance, reverse = d_start, False
            else:
                distance, reverse = d_end, True
            if distance < best_distance:
                best_index, best_distance, best_reverse = index, distance, reverse
        current = world.pop(best_index)
        if best_reverse:
            current = list(reversed(current))
        ordered.append(current)
    return ordered


def outline_waypoints(binary: np.ndarray, cfg: PlotConfig) -> list[tuple[float, float, float]]:
    """提取外轮廓，按最近邻排序，抽稀后逐轮廓落笔绘制。"""
    segments = _marching_squares_segments(binary)
    polylines = _segments_to_polylines(segments)
    if not polylines:
        return []

    # 每个折线转换为 (x_mm, y_mm) 点序
    world = [
        [_pixel_to_world(px, py, cfg) for px, py in poly]
        for poly in polylines
    ]

    ordered = _order_polylines(world)

    tol = cfg.simplify_px * cfg.pixel_to_mm
    points: list[tuple[float, float, float]] = []
    for poly in ordered:
        poly = _simplify_loop(poly, tol)
        if len(poly) < 2:
            continue
        x0, y0 = poly[0]
        last_x, last_y = poly[-1]
        points.append((x0, y0, cfg.z_up))
        points.append((x0, y0, cfg.z_down))
        for x, y in poly[1:]:
            points.append((x, y, cfg.z_down))
        # An open contour (ink clipped by the image border) must stop where
        # it ends. Returning to poly[0] here used to draw a straight chord
        # across the artwork that is not present in the image. Closed
        # loops already end at their start, so this is also the start there.
        points.append((last_x, last_y, cfg.z_up))
    return points


def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 纸张适配：图片 -> PlotConfig 参数
# ---------------------------------------------------------------------------
def fit_to_paper(
    img_w_px: int,
    img_h_px: int,
    paper_w_mm: float,
    paper_h_mm: float,
    margin_mm: float = 5.0,
    width_mm: float | None = None,
    height_mm: float | None = None,
    fill: float = 0.8,
) -> tuple[float, float, float, float, float]:
    """把一张图折算成 PlotConfig 需要的五个参数，图像**等比、居中**。

    返回 (pixel_to_mm, offset_x_mm, offset_y_mm, draw_w_mm, draw_h_mm)。

    规则：
        - 位置：永远居中，中心 = (paper_w/2, paper_h/2)；
        - 宽高比：永远按传入图片的原始比例，不拉伸；
        - 大小：给了 width_mm/height_mm 就等比缩进这个框；都不给就缩进
          (paper * fill) 的框，默认占纸面 80%；
        - 兜底：任何情况都不超出 (paper - 2*margin) 的可用区。

    典型用法::

        scale, ox, oy, w, h = fit_to_paper(
            bin.shape[1], bin.shape[0], paper_w_mm=paper_w, paper_h_mm=paper_h
        )
        cfg = PlotConfig(pixel_to_mm=scale, offset_x=ox, offset_y=oy)
        points = to_waypoints(bin, cfg)
    """
    usable_w = paper_w_mm - 2.0 * margin_mm
    usable_h = paper_h_mm - 2.0 * margin_mm
    if usable_w <= 0 or usable_h <= 0:
        raise ValueError("边距太大，纸张没有可用区域")

    if width_mm or height_mm:
        box_w = float(width_mm) if width_mm else usable_w
        box_h = float(height_mm) if height_mm else usable_h
    else:
        box_w = min(paper_w_mm * fill, usable_w)
        box_h = min(paper_h_mm * fill, usable_h)

    # 等比：只由图片自身宽高比决定
    scale = min(box_w / img_w_px, box_h / img_h_px)
    scale = min(scale, usable_w / img_w_px, usable_h / img_h_px)

    draw_w = img_w_px * scale
    draw_h = img_h_px * scale
    offset_x = (paper_w_mm - draw_w) / 2.0
    offset_y = (paper_h_mm - draw_h) / 2.0
    return scale, offset_x, offset_y, draw_w, draw_h


def to_waypoints(
    binary: np.ndarray, cfg: PlotConfig | None = None
) -> list[tuple[float, float, float]]:
    """二值图 -> 轨迹点（唯一策略：轮廓细描）。"""
    return outline_waypoints(binary, cfg or PlotConfig())
