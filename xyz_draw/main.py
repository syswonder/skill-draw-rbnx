# SPDX-License-Identifier: MIT
"""xyz_draw — MCP drawing skill for the XYZ pen plotter.

Owns ``robonix/skill/plot/*``:

  * ``driver``         shared lifecycle contract.
  * ``config``         read-only policy flags (debug, imagegen_ready,
                       preview name, home_before_draw) for the planner.
  * ``render_preview`` declarative JSON scene -> PNG preview in
                       ``output_dir`` (only when config ``debug=true``).
  * ``draw_scene``     declarative JSON scene -> waypoints, no raster step;
                       homes first and writes ``last_preview.png`` first
                       when ``debug=true``.
  * ``draw_prompt``    text prompt -> imagegen service -> PNG -> waypoints
                       (only when config ``imagegen_enabled=true``).
  * ``draw_image``     PNG from ``image_dir``/``output_dir`` -> waypoints.
  * ``list_images``    discover drawable files without guessing paths.

Homing and global motion parameters are exposed by the plotter primitive
(``robonix/primitive/plotter/{home,set_params}``), not by this skill: an
LLM/user changes them directly, so drawing never overwrites them.

Text writing lives in the separate ``skill-text-rbnx`` package. This
skill never executes model-written Python: preview rendering is a fixed
JSON-scene renderer, drawing is either a fixed vector conversion
(``draw_scene``) or a fixed raster-tracing pipeline (``draw_image``).

The vendored geometry package (``xyz_draw/geometry/{image,trajectory}.py``)
does the outline tracing and paper fitting. This module orchestrates the
primitive over gRPC; it never touches the PLC or the TCP protocol.

Coordinate conventions come from the plotter primitive: millimetres, paper
origin at the lower-left corner, +X right, +Y up. The drawable area and
pen heights are read from ``robonix/primitive/plotter/geometry`` once on
activation (falling back to the configured workspace).
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
import time
from dataclasses import dataclass

from robonix_api import Deferred, Err, Ok, Skill

xyz_draw = Skill(id="xyz_draw", namespace="robonix/skill/plot")

from plot_mcp import (  # type: ignore  # noqa: E402  (rbnx codegen)
    Config_Request,
    Config_Response,
    DrawImage_Request,
    DrawImage_Response,
    DrawPrompt_Request,
    DrawPrompt_Response,
    DrawScene_Request,
    DrawScene_Response,
    GenerateImage_Request,
    GenerateImage_Response,
    ListImages_Request,
    ListImages_Response,
)

from . import image  # noqa: E402
from . import imagegen  # noqa: E402
from .generate import (  # noqa: E402
    GenerateError,
    GenerateOptions,
    generate_scene,
)
from .metadata import read_png_hints, resolve_flip_y  # noqa: E402
from .paths import (  # noqa: E402
    DEFAULT_IMAGE_DIR,
    DEFAULT_PREVIEW_NAME,
    DEFAULT_SUFFIXES,
    ImagePathError,
    ImageStore,
)
from .plan import (  # noqa: E402
    PlanError,
    Workspace,
    describe_plan,
    validate_waypoints,
)
from .primitive_client import (  # noqa: E402
    PrimitiveCallError,
    PrimitiveClient,
    PrimitiveDiscoveryError,
    resolve_endpoints,
)
from .scene import scene_to_waypoints  # noqa: E402

log = logging.getLogger("xyz_draw")
logging.basicConfig(
    level=logging.INFO,
    format="[xyz_draw] %(levelname)s %(message)s",
)
# basicConfig is a no-op when the runtime already installed root handlers,
# so pin this logger's level explicitly to keep job logs visible.
log.setLevel(logging.INFO)


@dataclass(frozen=True)
class SkillConfig:
    workspace: Workspace
    auto_home: bool
    #: When true, draw_scene/draw_image/draw_prompt run the primitive HOME
    #: routine before sending a trajectory. Planners that already called
    #: plotter_home pass skip_home=true to avoid repeating it.
    home_before_draw: bool
    #: When true, render_preview is enabled and writes PNG previews to
    #: output_dir. When false, render_preview returns an error and callers
    #: draw directly with draw_scene / draw_image.
    debug: bool
    #: When true, draw_prompt may call an OpenAI-compatible text-to-image
    #: service. Disabled calls return success=false without any network I/O.
    imagegen_enabled: bool
    imagegen_base_url: str
    imagegen_model: str
    imagegen_api_key: str
    imagegen_size: str
    imagegen_timeout_s: float
    imagegen_prompt_suffix: str
    simplify_px: float
    image_margin_mm: float
    image_fill: float
    #: Default tracing mode: auto, outline, skeleton or lineart.
    trace: str
    trace_lineart_sigma: float
    trace_lineart_detail: float
    trace_spur_px: float
    trace_min_length_mm: float
    trace_bridge_px: float
    image_store: ImageStore
    generate_default_width_mm: float
    generate_default_height_mm: float
    generate_pixel_to_mm: float
    generate_line_width_mm: float
    generate_supersample: int
    generate_max_pixels: int
    generate_max_spec_bytes: int
    generate_max_strokes: int
    generate_max_points: int
    resolve_timeout_s: float
    max_points: int
    trajectory_timeout_s: float


_state = {
    "config": None,
    "client": None,
    "active": False,
}


# ── config parsing ───────────────────────────────────────────────────
def _num(
    cfg: dict,
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
):
    raw = cfg.get(key, default)
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as exc:
        kind = "an integer" if integer else "a number"
        raise ValueError(f"{key} must be {kind}, got {raw!r}") from exc
    if not integer and not math.isfinite(value):
        raise ValueError(f"{key} must be finite, got {value}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} must be <= {maximum}, got {value}")
    return value


def _bool(cfg: dict, key: str, default: bool) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _text(cfg: dict, key: str, default: str) -> str:
    raw = cfg.get(key, default)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a string, got {type(raw).__name__}")
    return raw.strip() or default


def _suffixes(cfg: dict) -> tuple[str, ...]:
    raw = cfg.get("allowed_image_suffixes", DEFAULT_SUFFIXES)
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError(
            "allowed_image_suffixes must be a list or comma-separated string"
        )
    cleaned = tuple(str(item).strip() for item in items if str(item).strip())
    if not cleaned:
        raise ValueError("allowed_image_suffixes must not be empty")
    return cleaned


def _parse_config(cfg: dict) -> SkillConfig:
    raw_workspace = cfg.get("workspace") or {}
    if not isinstance(raw_workspace, dict):
        raise ValueError(
            f"workspace must be a mapping, got {type(raw_workspace).__name__}"
        )
    # The workspace geometry (drawable area + pen heights) is authoritative
    # in the plotter primitive and is fetched on activate. Config values are
    # neutral placeholders (all-zero) and are NOT validated here; the real
    # geometry replaces them once the primitive is reachable.
    workspace = Workspace(
        x_min=_num(raw_workspace, "x_min", 0.0),
        x_max=_num(raw_workspace, "x_max", 0.0),
        y_min=_num(raw_workspace, "y_min", 0.0),
        y_max=_num(raw_workspace, "y_max", 0.0),
        z_up=_num(raw_workspace, "z_up", 0.0),
        z_down=_num(raw_workspace, "z_down", 0.0),
    )

    paper_w = workspace.x_max - workspace.x_min
    paper_h = workspace.y_max - workspace.y_min
    default_w = _num(
        cfg, "generate_default_width_mm", paper_w, minimum=0.0
    )
    default_h = _num(
        cfg, "generate_default_height_mm", paper_h, minimum=0.0
    )
    if default_w <= 0.0:
        default_w = paper_w
    if default_h <= 0.0:
        default_h = paper_h
    if default_w > paper_w + 1e-9 or default_h > paper_h + 1e-9:
        raise ValueError(
            "generate_default_width_mm/height_mm must fit inside the "
            f"workspace ({paper_w:g} x {paper_h:g} mm), got "
            f"{default_w:g} x {default_h:g}"
        )

    image_store = ImageStore(
        _text(cfg, "image_dir", DEFAULT_IMAGE_DIR),
        _text(cfg, "output_dir", ""),
        allowed_suffixes=_suffixes(cfg),
        max_list_images=_num(
            cfg, "max_list_images", 200, minimum=1, integer=True
        ),
    )

    trace = _text(cfg, "trace", "auto").strip().lower()
    if trace not in ("auto", "outline", "skeleton", "lineart"):
        raise ValueError(
            "trace must be one of auto, outline, skeleton, lineart; "
            f"got {trace!r}"
        )

    imagegen_api_key = _text(cfg, "imagegen_api_key", "")
    if not imagegen_api_key:
        imagegen_api_key = (
            os.environ.get("IMAGE_GEN_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )

    return SkillConfig(
        workspace=workspace,
        auto_home=_bool(cfg, "auto_home", False),
        home_before_draw=_bool(cfg, "home_before_draw", True),
        debug=_bool(cfg, "debug", False),
        imagegen_enabled=_bool(cfg, "imagegen_enabled", False),
        imagegen_base_url=_text(cfg, "imagegen_base_url", ""),
        imagegen_model=_text(cfg, "imagegen_model", ""),
        imagegen_api_key=imagegen_api_key,
        imagegen_size=_text(cfg, "imagegen_size", "1024x1024"),
        imagegen_timeout_s=_num(
            cfg, "imagegen_timeout_s", 120.0, minimum=1.0, maximum=1800.0
        ),
        imagegen_prompt_suffix=_text(cfg, "imagegen_prompt_suffix", ""),
        simplify_px=_num(cfg, "simplify_px", 1.5, minimum=0.0),
        image_margin_mm=_num(cfg, "image_margin_mm", 5.0, minimum=0.0),
        image_fill=_num(cfg, "image_fill", 0.8, minimum=0.0, maximum=1.0),
        trace=trace,
        trace_lineart_sigma=_num(
            cfg, "trace_lineart_sigma", 1.2, minimum=0.1, maximum=50.0
        ),
        trace_lineart_detail=_num(
            cfg, "trace_lineart_detail", 1.0, minimum=0.0, maximum=10.0
        ),
        trace_spur_px=_num(cfg, "trace_spur_px", 2.0, minimum=0.0),
        trace_min_length_mm=_num(
            cfg, "trace_min_length_mm", 1.0, minimum=0.0
        ),
        trace_bridge_px=_num(
            cfg, "trace_bridge_px", 3.0, minimum=0.0, maximum=20.0
        ),
        image_store=image_store,
        generate_default_width_mm=default_w,
        generate_default_height_mm=default_h,
        generate_pixel_to_mm=_num(
            cfg, "generate_pixel_to_mm", 0.25, minimum=0.005, maximum=10.0
        ),
        generate_line_width_mm=_num(
            cfg, "generate_line_width_mm", 0.5, minimum=0.01, maximum=10.0
        ),
        generate_supersample=_num(
            cfg, "generate_supersample", 2, minimum=1, maximum=4, integer=True
        ),
        generate_max_pixels=_num(
            cfg, "generate_max_pixels", 4096, minimum=64, maximum=8192,
            integer=True,
        ),
        generate_max_spec_bytes=_num(
            cfg, "generate_max_spec_bytes", 1048576, minimum=1024,
            maximum=16777216, integer=True,
        ),
        generate_max_strokes=_num(
            cfg, "generate_max_strokes", 20000, minimum=1, maximum=1000000,
            integer=True,
        ),
        generate_max_points=_num(
            cfg, "generate_max_points", 200000, minimum=1, maximum=2000000,
            integer=True,
        ),
        resolve_timeout_s=_num(cfg, "resolve_timeout_s", 60.0, minimum=0.0),
        max_points=_num(cfg, "max_points", 50000, minimum=1, integer=True),
        trajectory_timeout_s=_num(
            cfg, "trajectory_timeout_s", 600.0, minimum=1.0
        ),
    )


def _config() -> SkillConfig:
    config = _state["config"]
    if config is None:
        raise RuntimeError("skill is not initialized")
    return config


def _client() -> PrimitiveClient:
    client = _state["client"]
    if client is None or not _state["active"]:
        raise RuntimeError("skill is not active; primitive endpoints are unresolved")
    return client


def _home_before_motion(client: PrimitiveClient, config: SkillConfig) -> None:
    """Run HOME before a trajectory unless the policy disables it.

    Raises ``PrimitiveCallError`` when homing fails so the caller aborts
    before moving. Planners that already called ``plotter_home`` set
    ``skip_home`` on the draw request and never reach this helper.
    """
    if not config.home_before_draw:
        return
    log.info("homing before draw")
    response = client.home(timeout_s=config.trajectory_timeout_s)
    if not response.success:
        raise PrimitiveCallError(
            f"home before drawing failed: {response.message}"
        )


# ── request -> plan ──────────────────────────────────────────────────
def _generate_options(
    config: SkillConfig,
    *,
    max_width_mm: float | None = None,
    max_height_mm: float | None = None,
    pixel_to_mm: float = 0.0,
    line_width_mm: float = 0.0,
    supersample: int = 0,
) -> GenerateOptions:
    """Build renderer/parser limits for the effective workspace.

    ``draw_scene`` may be called with a workspace override smaller than the
    configured one, so the default canvas is clamped instead of failing
    ``width_mm must be <= paper`` inside ``parse_scene``. The renderer
    fields are irrelevant to ``draw_scene`` but share the same parser.
    """
    if max_width_mm is None:
        paper_w = config.workspace.x_max - config.workspace.x_min
    else:
        paper_w = float(max_width_mm)
    if max_height_mm is None:
        paper_h = config.workspace.y_max - config.workspace.y_min
    else:
        paper_h = float(max_height_mm)
    return GenerateOptions(
        default_width_mm=min(config.generate_default_width_mm, paper_w),
        default_height_mm=min(config.generate_default_height_mm, paper_h),
        max_width_mm=paper_w,
        max_height_mm=paper_h,
        pixel_to_mm=(
            float(pixel_to_mm)
            if float(pixel_to_mm) > 0.0
            else config.generate_pixel_to_mm
        ),
        line_width_mm=(
            float(line_width_mm)
            if float(line_width_mm) > 0.0
            else config.generate_line_width_mm
        ),
        supersample=(
            int(supersample) if int(supersample) > 0 else config.generate_supersample
        ),
        max_pixels=config.generate_max_pixels,
        max_spec_bytes=config.generate_max_spec_bytes,
        max_strokes=config.generate_max_strokes,
        max_points=config.generate_max_points,
    )


def _workspace_from_request(req, fallback: Workspace) -> Workspace:
    raw = getattr(req, "workspace", None)
    if raw is None:
        return fallback
    x_min = float(raw.x_min)
    x_max = float(raw.x_max)
    y_min = float(raw.y_min)
    y_max = float(raw.y_max)
    # Default (all-zero) or malformed rectangles mean "use config".
    if x_max <= x_min or y_max <= y_min:
        return fallback
    z_up = float(raw.z_up)
    z_down = float(raw.z_down)
    if z_up <= z_down:
        z_up, z_down = fallback.z_up, fallback.z_down
    workspace = Workspace(
        x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
        z_up=z_up, z_down=z_down,
    )
    workspace.validate()
    return workspace


def _orientation(req, hints) -> bool:
    """Resolve the effective vertical orientation to a ``flip_y`` flag."""
    return resolve_flip_y(
        getattr(req, "orientation", ""),
        hints,
        flip_y=bool(getattr(req, "flip_y", False)),
    )


def _trace_mode(req, config: SkillConfig, hints) -> str:
    """Resolve ``trace`` to outline/skeleton/lineart.

    ``auto`` uses ``skeleton`` for PNGs produced by render_preview (they
    carry Robonix metadata, so they are known line art) and ``outline``
    for third-party images, preserving the previous default.
    """
    raw = str(getattr(req, "trace", "") or "").strip().lower()
    if not raw:
        raw = config.trace
    if raw in ("", "auto"):
        return "skeleton" if hints.orientation == "screen" else "outline"
    if raw not in image.TRACE_MODES:
        raise ValueError(
            f"unsupported trace mode {raw!r}; use one of "
            f"{', '.join(image.TRACE_MODES)}"
        )
    return raw


def _image_response(
    success: bool,
    message: str,
    started: float,
    *,
    plan=None,
    image_path: str = "",
    pixel_to_mm: float = 0.0,
    flip_y: bool = False,
    trace: str = "",
) -> DrawImage_Response:
    return DrawImage_Response(
        success=success,
        message=message,
        points=plan.point_count if plan is not None else 0,
        image_path=image_path,
        pixel_to_mm=float(pixel_to_mm),
        flip_y=bool(flip_y),
        trace=str(trace),
        min_x=plan.min_x if plan is not None else 0.0,
        max_x=plan.max_x if plan is not None else 0.0,
        min_y=plan.min_y if plan is not None else 0.0,
        max_y=plan.max_y if plan is not None else 0.0,
        elapsed_s=time.monotonic() - started,
    )


def _generate_response(
    success: bool,
    message: str,
    started: float,
    *,
    generated=None,
) -> GenerateImage_Response:
    image_path = (
        str(generated.path)
        if generated is not None and generated.path is not None
        else ""
    )
    return GenerateImage_Response(
        success=success,
        message=message,
        image_path=image_path,
        width_px=generated.width_px if generated is not None else 0,
        height_px=generated.height_px if generated is not None else 0,
        width_mm=generated.width_mm if generated is not None else 0.0,
        height_mm=generated.height_mm if generated is not None else 0.0,
        pixel_to_mm=generated.pixel_to_mm if generated is not None else 0.0,
        strokes=generated.stroke_count if generated is not None else 0,
        points=generated.point_count if generated is not None else 0,
        elapsed_s=time.monotonic() - started,
    )


def _scene_response(
    success: bool,
    message: str,
    started: float,
    *,
    plan=None,
    scene_plan=None,
) -> DrawScene_Response:
    return DrawScene_Response(
        success=success,
        message=message,
        points=plan.point_count if plan is not None else 0,
        strokes=scene_plan.stroke_count if scene_plan is not None else 0,
        width_mm=scene_plan.width_mm if scene_plan is not None else 0.0,
        height_mm=scene_plan.height_mm if scene_plan is not None else 0.0,
        min_x=plan.min_x if plan is not None else 0.0,
        max_x=plan.max_x if plan is not None else 0.0,
        min_y=plan.min_y if plan is not None else 0.0,
        max_y=plan.max_y if plan is not None else 0.0,
        elapsed_s=time.monotonic() - started,
    )


# ── response helpers ─────────────────────────────────────────────────
def _prompt_response(
    success: bool,
    message: str,
    started: float,
    *,
    image_path: str = "",
    width_px: int = 0,
    height_px: int = 0,
    draw_response=None,
) -> DrawPrompt_Response:
    if (
        not success
        and message
        and "draw_scene" not in message
        and message != "prompt is empty"
    ):
        message = f"{message}; fall back to draw_scene with a simple declarative scene"
    return DrawPrompt_Response(
        success=success,
        message=message,
        image_path=image_path,
        width_px=width_px,
        height_px=height_px,
        points=draw_response.points if draw_response is not None else 0,
        pixel_to_mm=(
            draw_response.pixel_to_mm if draw_response is not None else 0.0
        ),
        flip_y=draw_response.flip_y if draw_response is not None else False,
        trace=draw_response.trace if draw_response is not None else "",
        min_x=draw_response.min_x if draw_response is not None else 0.0,
        max_x=draw_response.max_x if draw_response is not None else 0.0,
        min_y=draw_response.min_y if draw_response is not None else 0.0,
        max_y=draw_response.max_y if draw_response is not None else 0.0,
        elapsed_s=time.monotonic() - started,
    )


# ── MCP tools ────────────────────────────────────────────────────────
@xyz_draw.mcp("robonix/skill/plot/config")
def plot_config(req: Config_Request) -> Config_Response:
    """Return the drawing policy flags. Read-only: no motion, no files.

    Call this before planning a drawing request.

    - ``debug``: render_preview is enabled and a non-dry_run draw_scene
      writes output_dir/last_preview.png before moving.
    - ``imagegen_ready``: draw_prompt has an enabled service, base URL and
      model, so it can be tried.
    - ``home_before_draw``: draw tools run the primitive HOME routine
      before the trajectory.

    Planning rules:

    1. Structured/geometric request (explicit shapes, coordinates or JSON):
       plotter_home -> render_preview (only when debug=true) -> draw_scene.
       Pass skip_home=true to draw_scene after plotter_home, and
       skip_preview=true when render_preview was just called.
    2. Free-form/illustrative request with imagegen_ready=true:
       plotter_home -> draw_prompt (skip_home=true). If draw_prompt fails,
       author a simple declarative scene and continue with rule 1.
    3. Preview failures never block drawing.

    The skill still enforces home/preview when the caller omits the
    skip flags, so the plan is advisory unless the flags are passed.
    """
    started = time.monotonic()
    try:
        config = _config()
        ready = bool(
            config.imagegen_enabled
            and config.imagegen_base_url.strip()
            and config.imagegen_model.strip()
        )
        return Config_Response(
            success=True,
            message=(
                f"debug={config.debug} imagegen_ready={ready} "
                f"home_before_draw={config.home_before_draw} "
                f"preview={DEFAULT_PREVIEW_NAME}"
            ),
            debug=config.debug,
            imagegen_enabled=config.imagegen_enabled,
            imagegen_ready=ready,
            image_dir=str(config.image_store.image_dir),
            output_dir=str(config.image_store.output_dir),
            preview_name=DEFAULT_PREVIEW_NAME,
            home_before_draw=config.home_before_draw,
            elapsed_s=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - never raise across the MCP boundary
        log.warning("plot_config failed: %s", exc)
        return Config_Response(
            success=False,
            message=str(exc) or exc.__class__.__name__,
            debug=False,
            imagegen_enabled=False,
            imagegen_ready=False,
            image_dir="",
            output_dir="",
            preview_name=DEFAULT_PREVIEW_NAME,
            home_before_draw=False,
            elapsed_s=time.monotonic() - started,
        )


@xyz_draw.mcp("robonix/skill/plot/render_preview")
def render_preview(req: GenerateImage_Request) -> GenerateImage_Response:
    """Render a declarative JSON scene to a PNG preview.

    This tool is the debug/preview path. It is enabled only when the skill
    config has ``debug: true``; with ``debug: false`` it returns
    ``success=false`` and does not write anything. To actually plot, use
    ``draw_scene`` (declarative scenes) or ``draw_image`` (existing image
    files). With ``debug=true``, ``draw_scene`` also writes the preview
    itself before moving, so calling this tool first is optional: use it
    when the preview should exist before the draw is planned, then pass
    ``skip_preview=true`` to ``draw_scene``.

    The scene is JSON; coordinates are millimetres on a canvas whose
    origin is the lower-left corner (+X right, +Y up). Example:

      {"width_mm": 180, "height_mm": 150,
       "strokes": [[[10,10], [20,20], [30,10]]],
       "shapes": [{"type": "circle", "cx": 90, "cy": 75, "r": 30},
                  {"type": "rect", "x": 10, "y": 10, "w": 40, "h": 20}]}

    Supported shapes: line, polyline/path, polygon, rect, circle, ellipse,
    arc. Each stroke/shape may set width_mm and closed. Strokes are drawn
    first, then shapes, in list order. Only line art: no fills, colours, or
    text. The renderer honours width_mm, antialiasing and supersampling. The
    PNG is a visual artifact only; draw_scene plots the same geometry as
    vectors and optionally renders this same preview when debug=true.

    The PNG is written only inside the configured output_dir; output_name
    is sanitised and forced to .png. An empty output_name selects the
    fixed preview slot ``last_preview.png`` and always overwrites it.
    A caller-supplied name keeps the normal rule: existing files are kept
    unless overwrite=true. The PNG carries orientation/scale metadata, so
    it can also be passed to draw_image when a rasterised result is
    explicitly wanted, but draw_scene is the preferred way to plot the
    same scene.

    This tool is optional when draw_scene is used: with debug=true,
    draw_scene writes the same last_preview.png before moving. Call this
    tool first when the preview should be available before planning the
    draw; pass skip_preview=true to draw_scene afterwards.
    """
    started = time.monotonic()
    if not str(req.spec or "").strip():
        return _generate_response(False, "spec is empty", started)

    try:
        config = _config()
        if not config.debug:
            return _generate_response(
                False,
                "render_preview is disabled because debug=false; use "
                "draw_scene for declarative scenes or draw_image for "
                "existing image files",
                started,
            )
        options = _generate_options(
            config,
            pixel_to_mm=float(req.pixel_to_mm),
            line_width_mm=float(req.line_width_mm),
            supersample=int(req.supersample),
        )
        output = config.image_store.resolve_preview(
            str(req.output_name or ""), overwrite=bool(req.overwrite)
        )
        generated = generate_scene(str(req.spec), output, options, write=True)
        log.info(
            "render_preview ok: %s (%dx%d px, %d strokes)",
            generated.path, generated.width_px, generated.height_px,
            generated.stroke_count,
        )
        message = (
            f"preview written to {generated.path} "
            f"({generated.width_px}x{generated.height_px} px, "
            f"{generated.stroke_count} strokes); use draw_scene to plot the "
            "same JSON"
        )
        return _generate_response(
            True,
            message,
            started,
            generated=generated,
        )
    except (GenerateError, ImagePathError, RuntimeError) as exc:
        log.warning("render_preview failed: %s", exc)
        return _generate_response(False, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - never raise across the MCP boundary
        log.exception("render_preview raised unexpectedly")
        return _generate_response(False, str(exc) or exc.__class__.__name__, started)


@xyz_draw.mcp("robonix/skill/plot/draw_image")
def draw_image(req: DrawImage_Request) -> DrawImage_Response:
    """Draw a PNG image with the XYZ pen plotter.

    The image must be inside the configured image_dir (or output_dir).
    Use list_images first when the user refers to an existing file; never
    invent a path outside those directories. Ink is dark by default;
    invert=true handles light ink on a dark background.

    trace selects the pen path strategy:
      'outline'  (SDK) trace the ink boundary. Correct for silhouettes and
                 filled shapes, but a line becomes two parallel sides.
      'skeleton' thin the ink to a centreline and trace that. Best for
                 line art: one pen stroke per drawn line.
      'lineart'  extract edges with a difference-of-Gaussians filter, then
                 skeletonise. Best for photos and grey artwork.
      '' / 'auto' use the skill config; auto selects skeleton for PNGs
                 produced by render_preview and outline otherwise.

    orientation='auto' (default) honours Robonix metadata embedded by
    render_preview: such images are drawn upright and at their stored
    millimetre scale. A PNG without metadata is also drawn screen-upright
    (the way it is displayed) and auto-fits to the paper. Force
    orientation='screen' for upright or 'paper' for the raw raster
    mapping. flip_x mirrors left/right; flip_y mirrors top/bottom of the
    screen orientation when orientation='auto' and the file has no
    metadata.

    scale=0 uses the metadata scale when present, otherwise auto-fits;
    scale<0 forces auto-fit; scale>0 is an explicit pixel-to-mm factor.
    margin_mm/fill control only automatic fitting. Rotation is not a
    parameter: rotate the image file beforehand. dry_run=true previews
    the waypoint count without moving the machine.

    Before the trajectory this tool runs the primitive HOME routine unless
    skip_home=true (set it when plotter_home was already called); after a
    successful draw auto_home runs HOME again.
    """
    started = time.monotonic()
    path_raw = str(req.image_path or "")
    if not path_raw.strip():
        return _image_response(False, "image_path is empty", started)

    try:
        config = _config()
        input_path = config.image_store.resolve_input(path_raw)
        workspace = _workspace_from_request(req, config.workspace)

        hints = read_png_hints(str(input_path))
        flip_y = _orientation(req, hints)
        trace_mode = _trace_mode(req, config, hints)

        requested_scale = float(req.scale)
        if requested_scale < 0.0:
            scale = 0.0
        elif requested_scale == 0.0 and hints.pixel_to_mm > 0.0:
            scale = hints.pixel_to_mm
        else:
            scale = requested_scale

        offset_x = float(req.offset_x)
        offset_y = float(req.offset_y)
        if offset_x == 0.0 and offset_y == 0.0 and hints.orientation:
            offset_x = hints.offset_x_mm
            offset_y = hints.offset_y_mm

        simplify_px = float(req.simplify_px)
        if simplify_px < 0.0:
            simplify_px = config.simplify_px
        fill = float(req.fill)
        if fill <= 0.0:
            fill = config.image_fill
        margin = float(req.margin_mm)
        if margin < 0.0:
            margin = config.image_margin_mm

        image_plan = image.image_to_waypoints_ex(
            str(input_path),
            workspace=workspace,
            scale=scale,
            offset=(offset_x, offset_y),
            threshold=float(req.threshold),
            invert=bool(req.invert),
            flip_x=bool(req.flip_x),
            flip_y=flip_y,
            simplify_px=simplify_px,
            margin_mm=margin,
            fill=fill,
            trace=trace_mode,
            lineart_sigma=config.trace_lineart_sigma,
            lineart_detail=config.trace_lineart_detail,
            spur_px=config.trace_spur_px,
            min_length_mm=config.trace_min_length_mm,
            bridge_px=config.trace_bridge_px,
        )
        plan = validate_waypoints(
            image_plan.points, workspace=workspace, max_points=config.max_points
        )
        placement = (
            f"scale={image_plan.pixel_to_mm:.5g} mm/px, "
            f"{'screen' if flip_y else 'paper'} orientation, "
            f"trace={trace_mode}"
        )
        plan_summary = describe_plan(plan)

        if req.dry_run:
            log.info(
                "draw_image dry run: %s; %s; %s",
                input_path, placement, plan_summary,
            )
            return _image_response(
                True,
                f"dry run: {plan.point_count} waypoints planned from "
                f"{input_path.name} ({placement}), machine not moved",
                started,
                plan=plan,
                image_path=str(input_path),
                pixel_to_mm=image_plan.pixel_to_mm,
                flip_y=flip_y,
                trace=trace_mode,
            )

        log.info(
            "draw_image sending: %s; %s; %s",
            input_path, placement, plan_summary,
        )
        client = _client()
        if not bool(getattr(req, "skip_home", False)):
            _home_before_motion(client, config)
        response = client.run_trajectory(
            plan.waypoints, timeout_s=config.trajectory_timeout_s
        )
        if not response.success:
            log.warning(
                "draw_image primitive stopped after %d/%d points: %s",
                response.points_completed,
                response.points_accepted,
                response.message,
            )
            return _image_response(
                False,
                "primitive stopped after "
                f"{response.points_completed}/{response.points_accepted} points: "
                f"{response.message}",
                started,
                plan=plan,
                image_path=str(input_path),
                pixel_to_mm=image_plan.pixel_to_mm,
                flip_y=flip_y,
                trace=trace_mode,
            )

        message = (
            f"drew {plan.point_count} waypoints from {input_path.name} "
            f"({placement})"
        )
        if req.auto_home or config.auto_home:
            home_response = client.home(timeout_s=config.trajectory_timeout_s)
            message += (
                "; returned home"
                if home_response.success
                else f"; auto home failed: {home_response.message}"
            )

        log.info("draw_image ok: %s; %s; %s", input_path, placement, plan_summary)
        return _image_response(
            True,
            message,
            started,
            plan=plan,
            image_path=str(input_path),
            pixel_to_mm=image_plan.pixel_to_mm,
            flip_y=flip_y,
            trace=trace_mode,
        )

    except (
        image.ImageError,
        ImagePathError,
        GenerateError,
        PlanError,
        PrimitiveCallError,
        PrimitiveDiscoveryError,
        RuntimeError,
        ValueError,
    ) as exc:
        log.warning("draw_image failed: %s", exc)
        return _image_response(False, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - never raise across the MCP boundary
        log.exception("draw_image raised unexpectedly")
        return _image_response(False, str(exc) or exc.__class__.__name__, started)


@xyz_draw.mcp("robonix/skill/plot/draw_scene")
def draw_scene(req: DrawScene_Request) -> DrawScene_Response:
    """Plot a declarative JSON scene directly, without rasterising it.

    Use this as the primary plotting tool. The JSON format is identical to
    render_preview: millimetres on a canvas whose origin is the lower-left
    corner (+X right, +Y up). Supported shapes are line, polyline/path,
    polygon, rect, circle, ellipse and arc; each stroke/shape may set
    width_mm and closed. Fills, colours and text are not supported (use
    the separate text skill for text).

    Every stroke becomes exactly one pen-down polyline. Unlike
    render_preview followed by draw_image, the plotted geometry is never
    rasterised and the ink is never contour-traced, so a line is drawn once
    (not as two parallel outline sides) and circles/arcs stay smooth.
    width_mm is ignored for plotting: the physical pen tip defines line
    width. The optional debug preview is only an artifact; it never feeds
    the trajectory.

    The canvas origin maps to the workspace origin plus offset_x/offset_y.
    width_mm/height_mm default to the config and must fit the workspace;
    every point is validated before any motion, so an invalid scene or a
    plan over max_points returns success=false without moving the machine.

    Before the trajectory this tool runs the primitive HOME routine unless
    skip_home=true (set it when plotter_home was already called). After a
    successful draw auto_home returns home.

    When debug=true, the same scene is also rendered to
    output_dir/last_preview.png before moving unless skip_preview=true
    (set it when render_preview was already called). A preview failure is
    reported in the message and never blocks drawing. dry_run=true never
    homes and never writes a preview.

    Example:
      {"width_mm": 180, "height_mm": 150,
       "strokes": [[[10,10], [20,20], [30,10]]],
       "shapes": [{"type": "circle", "cx": 90, "cy": 75, "r": 30},
                  {"type": "rect", "x": 10, "y": 10, "w": 40, "h": 20}]}
    """
    started = time.monotonic()
    if not str(req.spec or "").strip():
        return _scene_response(False, "spec is empty", started)

    try:
        config = _config()
        workspace = _workspace_from_request(req, config.workspace)
        options = _generate_options(
            config,
            max_width_mm=workspace.x_max - workspace.x_min,
            max_height_mm=workspace.y_max - workspace.y_min,
        )
        scene_plan = scene_to_waypoints(
            str(req.spec),
            options,
            workspace,
            offset=(float(req.offset_x), float(req.offset_y)),
        )
        plan = validate_waypoints(
            scene_plan.points,
            workspace=workspace,
            max_points=config.max_points,
        )
        summary = (
            f"{scene_plan.stroke_count} strokes, {plan.point_count} waypoints, "
            f"{scene_plan.width_mm:g}x{scene_plan.height_mm:g} mm canvas"
        )
        plan_summary = describe_plan(plan)

        if req.dry_run:
            log.info("draw_scene dry run: %s; %s", summary, plan_summary)
            return _scene_response(
                True,
                f"dry run: {summary}; machine not moved",
                started,
                plan=plan,
                scene_plan=scene_plan,
            )

        log.info("draw_scene sending: %s; %s", summary, plan_summary)
        client = _client()
        if not bool(getattr(req, "skip_home", False)):
            _home_before_motion(client, config)

        preview_note = ""
        if config.debug and not bool(getattr(req, "skip_preview", False)):
            try:
                preview_output = config.image_store.resolve_preview()
                generated = generate_scene(
                    str(req.spec), preview_output, options, write=True
                )
                preview_note = f"; preview written to {generated.path}"
                log.info("draw_scene preview: %s", generated.path)
            except Exception as exc:  # noqa: BLE001 - preview never blocks
                log.warning("draw_scene preview failed (continuing): %s", exc)
                preview_note = f"; preview failed (continuing): {exc}"

        response = client.run_trajectory(
            plan.waypoints, timeout_s=config.trajectory_timeout_s
        )
        if not response.success:
            log.warning(
                "draw_scene primitive stopped after %d/%d points: %s",
                response.points_completed,
                response.points_accepted,
                response.message,
            )
            return _scene_response(
                False,
                "primitive stopped after "
                f"{response.points_completed}/{response.points_accepted} points: "
                f"{response.message}{preview_note}",
                started,
                plan=plan,
                scene_plan=scene_plan,
            )

        message = f"drew {summary}{preview_note}"
        if req.auto_home or config.auto_home:
            home_response = client.home(timeout_s=config.trajectory_timeout_s)
            message += (
                "; returned home"
                if home_response.success
                else f"; auto home failed: {home_response.message}"
            )

        log.info("draw_scene ok: %s; %s", summary, plan_summary)
        return _scene_response(
            True, message, started, plan=plan, scene_plan=scene_plan
        )

    except (
        ImagePathError,
        GenerateError,
        PlanError,
        PrimitiveCallError,
        PrimitiveDiscoveryError,
        RuntimeError,
        ValueError,
    ) as exc:
        log.warning("draw_scene failed: %s", exc)
        return _scene_response(False, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - never raise across the MCP boundary
        log.exception("draw_scene raised unexpectedly")
        return _scene_response(False, str(exc) or exc.__class__.__name__, started)


@xyz_draw.mcp("robonix/skill/plot/draw_prompt")
def draw_prompt(req: DrawPrompt_Request) -> DrawPrompt_Response:
    """Generate an image from a text prompt and draw it with the pen.

    Use this as the preferred path for free-form/illustrative requests
    (for example "draw a cat") when imagegen is ready; check plot_config
    first. When the request specifies exact geometry, coordinates or JSON,
    use draw_scene instead.

    Every successful generation writes the PNG into ``output_dir`` before
    tracing happens, even if the later drawing step fails. On any failure,
    fall back to draw_scene with a simple declarative scene authoring the
    geometry from the request; do not retry the same prompt.

    This tool is disabled unless the skill config has
    ``imagegen_enabled=true``. When disabled or misconfigured it returns
    ``success=false`` without making a network request.

    ``trace`` defaults to ``lineart`` (photos/grey artwork -> edge line art).
    Use ``skeleton`` for clean line art or ``outline`` for filled shapes.
    Generated PNGs are tagged screen-upright, so this tool draws them
    screen-upright by default; pass ``flip=true`` to mirror vertically.
    ``dry_run=true`` still generates the PNG and plans the waypoints, but
    does not move the machine. ``output_name`` is sanitised to one .png file
    inside ``output_dir``; ``overwrite=true`` replaces an existing file.

    Before the trajectory, HOME runs unless skip_home=true (set it when
    plotter_home was already called); auto_home runs after a successful
    draw.
    """
    started = time.monotonic()
    prompt = str(req.prompt or "").strip()
    if not prompt:
        return _prompt_response(False, "prompt is empty", started)

    try:
        config = _config()
        if not config.imagegen_enabled:
            return _prompt_response(
                False,
                "draw_prompt is disabled because imagegen_enabled=false; "
                "use draw_scene for declarative scenes or draw_image for "
                "existing image files",
                started,
            )
        if not config.imagegen_base_url:
            return _prompt_response(
                False,
                "imagegen_base_url is empty; set it in the skill config",
                started,
            )
        if not config.imagegen_model:
            return _prompt_response(
                False,
                "imagegen_model is empty; set it in the skill config",
                started,
            )

        trace = str(req.trace or "").strip().lower() or "lineart"
        if trace not in (*image.TRACE_MODES, "auto"):
            return _prompt_response(
                False,
                f"unsupported trace mode {trace!r}; use lineart, skeleton, "
                "outline, or auto",
                started,
            )

        # Resolve the filename before calling the service so a name clash
        # fails fast instead of after paying for an image generation.
        output = config.image_store.resolve_output(
            str(req.output_name or ""), overwrite=bool(req.overwrite)
        )

        full_prompt = prompt
        if config.imagegen_prompt_suffix:
            full_prompt = f"{prompt}, {config.imagegen_prompt_suffix}"
        options = imagegen.ImageGenOptions(
            base_url=config.imagegen_base_url,
            model=config.imagegen_model,
            api_key=config.imagegen_api_key,
            size=config.imagegen_size,
            timeout_s=config.imagegen_timeout_s,
        )
        raw = imagegen.generate_image(
            full_prompt, options, size=str(req.size or "")
        )
        width_px, height_px = imagegen.write_png(
            raw, output, max_pixels=config.generate_max_pixels
        )

        draw_req = DrawImage_Request(
            image_path=str(output),
            trace=trace,
            # Generated files are tagged screen-upright. Draw them that way by
            # default; flip=true selects the raw paper-raster direction
            # (vertically mirrored).
            orientation="paper" if bool(req.flip) else "screen",
            simplify_px=-1.0,
            fill=-1.0,
            margin_mm=-1.0,
            dry_run=bool(req.dry_run),
            auto_home=bool(req.auto_home),
            skip_home=bool(getattr(req, "skip_home", False)),
            workspace=req.workspace,
        )
        result = draw_image(draw_req)
        prefix = f"generated {output.name} ({width_px}x{height_px} px)"
        if not result.success:
            log.warning(
                "draw_prompt drawing failed: %s -> %s; %s",
                prompt[:80], output, result.message,
            )
            return _prompt_response(
                False,
                f"{prefix}; drawing failed: {result.message}",
                started,
                image_path=str(output),
                width_px=width_px,
                height_px=height_px,
                draw_response=result,
            )

        log.info(
            "draw_prompt ok: %s -> %s (%d points, trace=%s)",
            prompt[:80], output, result.points, trace,
        )
        return _prompt_response(
            True,
            f"{prefix}; {result.message}",
            started,
            image_path=str(output),
            width_px=width_px,
            height_px=height_px,
            draw_response=result,
        )

    except (
        imagegen.ImageGenError,
        ImagePathError,
        PlanError,
        PrimitiveCallError,
        PrimitiveDiscoveryError,
        RuntimeError,
        ValueError,
    ) as exc:
        log.warning("draw_prompt failed: %s", exc)
        return _prompt_response(False, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - never raise across the MCP boundary
        log.exception("draw_prompt raised unexpectedly")
        return _prompt_response(
            False, str(exc) or exc.__class__.__name__, started
        )


@xyz_draw.mcp("robonix/skill/plot/list_images")
def list_images(req: ListImages_Request) -> ListImages_Response:
    """List image files that draw_image is allowed to read.

    Searches image_dir and output_dir (non-recursively), sorted by
    filename. filter is an optional case-insensitive filename substring.
    Use this whenever the user says "draw the picture you just made" or
    names a file, then pass one of the returned absolute paths to
    draw_image. Supported suffixes are configured (PNG/JPEG/BMP/WebP by
    default).
    """
    started = time.monotonic()
    try:
        config = _config()
        files = config.image_store.list_images(str(req.filter or ""))
        roots = ", ".join(str(root) for root in config.image_store.read_dirs)
        message = (
            f"found {len(files)} image(s) in {roots}"
            if files
            else f"no images found in {roots}"
        )
        return ListImages_Response(
            success=True,
            message=message,
            images=[str(path) for path in files],
            count=len(files),
            image_dir=str(config.image_store.image_dir),
            output_dir=str(config.image_store.output_dir),
            elapsed_s=time.monotonic() - started,
        )
    except (ImagePathError, RuntimeError) as exc:
        log.warning("list_images failed: %s", exc)
        return ListImages_Response(
            success=False,
            message=str(exc),
            images=[],
            count=0,
            image_dir="",
            output_dir="",
            elapsed_s=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("list_images raised unexpectedly")
        return ListImages_Response(
            success=False,
            message=str(exc) or exc.__class__.__name__,
            images=[],
            count=0,
            image_dir="",
            output_dir="",
            elapsed_s=time.monotonic() - started,
        )


# ── lifecycle ────────────────────────────────────────────────────────
@xyz_draw.on_init
def init(cfg: dict):
    """REGISTERED -> INACTIVE. Parse and validate config only."""
    try:
        config = _parse_config(cfg or {})
    except (TypeError, ValueError, PlanError) as exc:
        return Err(f"invalid xyz_draw config: {exc}")

    _state["config"] = config
    log.info(
        "init ok: workspace x[%.1f, %.1f] y[%.1f, %.1f] z_up=%.2f z_down=%.2f",
        config.workspace.x_min, config.workspace.x_max,
        config.workspace.y_min, config.workspace.y_max,
        config.workspace.z_up, config.workspace.z_down,
    )
    log.info(
        "image roots: image_dir=%s output_dir=%s debug=%s "
        "home_before_draw=%s imagegen_enabled=%s preview=%s",
        config.image_store.image_dir, config.image_store.output_dir,
        config.debug, config.home_before_draw, config.imagegen_enabled,
        DEFAULT_PREVIEW_NAME,
    )
    return Ok()


@xyz_draw.on_activate
def activate():
    """INACTIVE -> ACTIVE. Resolve the plotter primitive endpoints."""
    config = _state["config"]
    if config is None:
        return Err("skill was not initialized")

    try:
        bindings = resolve_endpoints(
            xyz_draw, timeout_s=config.resolve_timeout_s
        )
    except PrimitiveDiscoveryError as exc:
        # Stay INACTIVE so the next MCP call retries activation instead of
        # parking the skill in ERROR. The primitive is declared first in the
        # deploy manifest, so this is normally a boot-order race only.
        log.warning("activate deferred: %s", exc)
        return Deferred(str(exc))

    old, _state["client"] = _state["client"], None
    if old is not None:
        old.close()
    client = PrimitiveClient(
        bindings, default_timeout_s=config.trajectory_timeout_s
    )
    _state["client"] = client
    try:
        geo = client.get_geometry()
        # Refresh both the workspace and the canvas default size, which is
        # derived from the drawable area reported by the primitive.
        _state["config"] = dataclasses.replace(
            config,
            workspace=Workspace(
                x_min=geo.x_min, x_max=geo.x_max,
                y_min=geo.y_min, y_max=geo.y_max,
                z_up=geo.z_up, z_down=geo.z_down,
            ),
            generate_default_width_mm=geo.x_max - geo.x_min,
            generate_default_height_mm=geo.y_max - geo.y_min,
        )
        log.info(
            "geometry from primitive: x_max=%.1f y_max=%.1f z_up=%.2f z_down=%.2f",
            geo.x_max, geo.y_max, geo.z_up, geo.z_down,
        )
    except Exception as exc:  # noqa: BLE001 - activation must never raise
        # Degrade instead of failing: a provider stuck in ERROR cannot be
        # activated again until the whole stack restarts.
        usable = (
            config.workspace.x_max > config.workspace.x_min
            and config.workspace.y_max > config.workspace.y_min
        )
        if usable:
            log.warning("geometry unavailable; using configured workspace: %s", exc)
        else:
            log.error(
                "geometry unavailable and no workspace configured; "
                "drawing will fail until one is set: %s", exc,
            )
    _state["active"] = True
    log.info(
        "activated: trajectory=%s home=%s",
        bindings["trajectory"].endpoint,
        bindings["home"].endpoint,
    )
    return Ok()


@xyz_draw.on_deactivate
def deactivate():
    """ACTIVE -> INACTIVE. Drop the primitive gRPC channels."""
    _state["active"] = False
    client, _state["client"] = _state["client"], None
    if client is not None:
        client.close()
    log.info("deactivated")
    return Ok()


@xyz_draw.on_shutdown
def shutdown():
    deactivate()
    log.info("shutdown")
    return Ok()


def main() -> int:
    xyz_draw.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())