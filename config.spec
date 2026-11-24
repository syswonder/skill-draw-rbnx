# Runtime config accepted by the XYZ drawing skill.
#
# This file documents the mapping passed as this package's `config:` value in
# robonix_manifest.yaml. Missing keys use the defaults below. Per-call MCP
# fields override these values; fields left at 0 / negative fall back to
# config as documented in each .srv.
#
# Everything is millimetres, matching the SDK and the PLC. Text writing lives
# in a separate package (skill-text-rbnx) and has its own config.

config:
  # Workspace geometry (drawable area + pen heights) is authoritative in the
  # plotter primitive and is read from its `geometry` contract on activation.
  # It is NOT configured here; a `workspace:` mapping in config is only a
  # fallback used when that contract is unreachable (the skill logs an error).

  # ── motion profile ─────────────────────────────────────────────────
  # Speed, acceleration, deceleration, and jerk are NOT skill config:
  # they are global PLC parameters owned by the plotter primitive. Set
  # them with the primitive's plotter_set_params MCP tool (or
  # robonix/primitive/plotter/set_params) before drawing. This skill only
  # sends waypoints, so a user-selected profile survives every job.
  #
  # Pen heights below (workspace.z_up/z_down) are planning geometry: the
  # skill encodes them into each waypoint's z, because the PLC uses the
  # per-point z rather than its own configured pen heights.

  # bool, default: false. If true, run the primitive home routine after
  # every successful draw_image or draw_scene. The per-call auto_home flag
  # ORs with this.
  auto_home: false

  # bool, default: true. If true, draw_scene/draw_image/draw_prompt run the
  # primitive HOME routine before sending a trajectory. plotter_home remains
  # a separate MCP tool: a planner that already homed should pass
  # skip_home=true so the routine is not repeated. auto_home above still
  # runs after a successful draw.
  home_before_draw: true

  # ── file roots ─────────────────────────────────────────────────────
  # draw_image and list_images read only these directories; render_preview
  # writes normal output only to output_dir. Relative paths are resolved
  # against ROBONIX_WORKSPACE when set, otherwise the deployment root (the
  # directory containing packages/). The default is <deployment>/images.
  image_dir: images

  # Empty means "same as image_dir". output_dir is always readable, so a
  # generated PNG can be passed straight back to draw_image.
  output_dir: ""

  # bool, default: false. Preview switch.
  # When true, render_preview is enabled and every non-dry_run draw_scene
  # also writes last_preview.png into output_dir before moving (unless the
  # call sets skip_preview=true). Preview failures are reported as warnings
  # and never block drawing. When false, render_preview returns success=false
  # and writes nothing; draw_scene stays side-effect free.
  # draw_scene dry_run=true never renders a PNG.
  debug: false

  # ── draw_prompt / image generation ─────────────────────────────────
  # bool, default: false. Enables the draw_prompt tool, which asks an
  # OpenAI-compatible image service for a PNG and then traces it with
  # draw_image. When false, draw_prompt returns success=false without any
  # network request; draw_scene/draw_image remain fully available.
  imagegen_enabled: false

  # string, default: "". OpenAI-compatible base URL, e.g.
  # http://127.0.0.1:8000/v1. draw_prompt POSTs to
  # <base>/images/generations; a full .../images/generations URL is also
  # accepted. Required when imagegen_enabled=true.
  imagegen_base_url: ""

  # string, default: "". Model name sent as "model" (e.g. dall-e-3,
  # flux-1.1-pro, or a local SD alias). Required when imagegen_enabled=true.
  imagegen_model: ""

  # string, default: "". Bearer token for the image service. When empty,
  # IMAGE_GEN_API_KEY / OPENAI_API_KEY from the environment are tried.
  imagegen_api_key: ""

  # string, default: "1024x1024". "size" passed to the service; the
  # per-call draw_prompt.size field overrides it. Empty omits the field.
  imagegen_size: "1024x1024"

  # float seconds, default: 120.0, 1..1800. Timeout for the generation
  # request and for downloading an image URL returned by the service.
  imagegen_timeout_s: 120.0

  # string, default: "". Appended to every prompt as ", <suffix>".
  # Useful for pen-friendly output, e.g.
  # "high contrast, clean white background, black line art, no text".
  imagegen_prompt_suffix: ""

  # list: suffixes accepted as drawable images (case-insensitive).
  allowed_image_suffixes: [".png", ".jpg", ".jpeg", ".bmp", ".webp"]

  # int >= 1, default: 200. Cap on list_images results.
  max_list_images: 200

  # ── draw_image defaults ────────────────────────────────────────────
  # float pixels, default: 1.5. Douglas-Peucker tolerance on traced
  # outlines/skeletons. 0 disables simplification; negative uses this value.
  simplify_px: 1.5

  # float mm, default: 5.0. Margin used by the automatic fit-to-paper.
  # Ignored when a positive scale or embedded metadata scale is used.
  image_margin_mm: 5.0

  # float 0..1, default: 0.8. Fraction of the paper the fitted image may
  # occupy, preserving aspect ratio. Ignored for 1:1 draws.
  image_fill: 0.8

  # string, default: "auto". Default tracing strategy for draw_image.
  #   auto      skeleton for PNGs written by render_preview (they carry
  #             Robonix metadata), outline for third-party images
  #   outline   ink boundary via marching squares — silhouettes/filled art
  #   skeleton  ink centreline via Zhang-Suen thinning — line art
  #   lineart   difference-of-Gaussians edges then skeleton — photos
  # The per-call draw_image.trace field overrides this value.
  trace: "auto"

  # float >= 0.1, default: 1.2. Gaussian sigma for the lineart edge filter.
  trace_lineart_sigma: 1.2

  # float 0..10, default: 1.0. lineart threshold in standard deviations
  # above the mean DoG response. Lower keeps more edges (and noise).
  # Weak edge pixels connected to strong ones are kept (hysteresis), so a
  # continuous contour is not cut where contrast dips.
  trace_lineart_detail: 1.0

  # float pixels, default: 2.0. Endpoint-peeling passes that prune short
  # skeleton spurs. Also trims the tip of every open line by this amount.
  trace_spur_px: 2.0

  # float mm, default: 1.0. Drop traced polylines shorter than this after
  # simplification. Removes raster noise and tiny skeleton loops.
  trace_min_length_mm: 1.0

  # float pixels, default: 3.0. Join neighbouring traced polylines whose
  # endpoints are at most this far apart. Fragmented skeletons otherwise
  # lift the pen at every gap, which looks dashed. 0 disables joining.
  trace_bridge_px: 3.0

  # ── preview renderer defaults ──────────────────────────────────────
  # Used by render_preview (debug=true) and by draw_scene when parsing a
  # scene (scene size limits and the default canvas).
  # Default canvas when the JSON scene omits width_mm/height_mm. 0 means
  # "use the drawable area reported by the plotter primitive". Any positive
  # value must fit inside the primitive's drawable area.
  generate_default_width_mm: 0.0
  generate_default_height_mm: 0.0

  # float mm per pixel, default: 0.25 (4 px/mm). 180 x 150 mm -> 720 x
  # 600 px. Smaller values give smoother outlines at the cost of PNG size.
  generate_pixel_to_mm: 0.25

  # float mm, default: 0.5. Ink width in the PNG. Thin lines are easier
  # for the outline tracer to turn into a single pen path.
  generate_line_width_mm: 0.5

  # int 1..4, default: 2. Supersampling factor before LANCZOS downscale.
  generate_supersample: 2

  # int, default: 4096. Reject a PNG whose width or height exceeds this.
  generate_max_pixels: 4096

  # int bytes, default: 1048576. Reject an oversized JSON scene string.
  generate_max_spec_bytes: 1048576

  # int, default: 20000. Reject scenes with more strokes/shapes.
  generate_max_strokes: 20000

  # int, default: 200000. Reject scenes with more source points.
  generate_max_points: 200000

  # ── limits and timeouts ────────────────────────────────────────────
  # float seconds, default: 60.0. How long activation waits for the
  # primitive contracts to appear in Atlas.
  resolve_timeout_s: 60.0

  # int, default: 50000. Reject a trajectory plan larger than this before
  # moving. Generated line art is traced, so dense scenes can exceed this.
  # draw_scene shares this limit (and generate_max_strokes/points for the
  # scene size). It does not need a PNG; a debug preview is written only
  # when debug=true unless skip_preview=true.
  max_points: 50000

  # float seconds, default: 600.0. Budget for the whole trajectory and
  # homing gRPC calls. The primitive estimates per-point timeouts on top
  # of this; a single job is still bounded by the PLC ACK.
  trajectory_timeout_s: 600.0