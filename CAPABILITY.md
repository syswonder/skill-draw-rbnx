---
description: Drawing skill — plot declarative JSON scenes directly, generate images from prompts, render debug PNG previews, trace images into pen strokes, and list drawable files.
---

# Drawing skill (`xyz_draw`)

Turns declarative scenes and raster images into waypoints and executes them
through the plotter primitive. It never executes model-written Python.

## MCP tools

### `plot_config`

Read-only policy flags for the planner. Call it before choosing a drawing
path; it never moves the machine and writes no files.

- `debug`: `plot_render_preview` is enabled, and every non-dry-run
  `plot_draw_scene` writes `output_dir/last_preview.png` before moving.
- `imagegen_ready`: `plot_draw_prompt` is enabled **and** has a base URL and
  model. `imagegen_enabled=true` alone is not enough.
- `home_before_draw`: draw tools run the primitive HOME routine before the
  trajectory.
- `preview_name` is fixed to `last_preview.png`.

Planning rules: structured/geometric requests use
`plotter_home → render_preview (debug only) → draw_scene`; free-form requests
with `imagegen_ready=true` use `plotter_home → draw_prompt`, falling back to
`draw_scene` on failure. Pass `skip_home=true` / `skip_preview=true` when a
step was already performed so the skill does not repeat it.

### `plot_draw_scene`

**Primary tool for drawing a declarative scene.** Convert the JSON scene
straight into pen-down polylines and plot it.

- `spec` (string, required): JSON object with `width_mm`, `height_mm`, and
  `strokes` and/or `shapes`. Supported shapes: `line`, `polyline`, `polygon`,
  `rect`, `circle`, `ellipse`, `arc`. Coordinates are millimetres relative to
  the canvas, origin at the lower-left, +Y up.
- The scene is **not** rasterised and no ink outline is traced: every stroke
  is drawn exactly once, and circles/arcs keep their requested geometry.
  `width_mm` is ignored for plotting because the pen tip defines line width.
- `offset_x` / `offset_y` translate the canvas origin inside the workspace.
- `dry_run=true` returns the stroke/waypoint counts and bounding box without
  moving; `auto_home=true` homes after a successful draw.
- The primitive HOME routine runs **before** the trajectory unless
  `skip_home=true`; after a successful draw `auto_home` homes again.
- With `debug=true`, the same scene is rendered to
  `output_dir/last_preview.png` before moving unless `skip_preview=true`.
  Preview failures are warnings and never block drawing. `dry_run=true`
  never homes and never writes a preview.
- `workspace` can override the configured rectangle and pen heights per call.

### `plot_draw_prompt`

Generate an image from a text prompt and draw it. This is the **preferred
path for free-form/illustrative requests** when `plot_config` reports
`imagegen_ready=true`; use `plot_draw_scene` when the request specifies
exact geometry, coordinates, or JSON.

- The skill calls the OpenAI-compatible image service configured by
  `imagegen_base_url` / `imagegen_model`, writes the returned PNG into
  `output_dir` (every successful generation is written before tracing),
  then traces it with the draw_image pipeline.
- **Disabled by default.** Requires the skill config
  `imagegen_enabled: true` plus a non-empty base URL and model. When disabled
  or misconfigured it returns `success=false` without a network request.
- `trace` defaults to `lineart`; `skeleton` and `outline` can be requested
  explicitly.
- Generated PNGs are tagged screen-upright (the same metadata
  `plot_render_preview` writes), so the tool draws them upright by default.
  Pass `flip=true` to draw the raw raster direction (vertically mirrored).
- `dry_run=true` still generates the PNG and plans waypoints but does not
  move the machine. `output_name` is sanitised to one `.png` file inside
  `output_dir`; `overwrite=true` replaces an existing file.
- On failure, fall back to `plot_draw_scene` with a simple declarative scene
  authored from the request instead of retrying the same prompt.
- The primitive HOME routine runs before the trajectory unless
  `skip_home=true`; `auto_home` homes after a successful draw.
- This is a separate text-to-image service, not the robot's VLM. The VLM
  can understand images but cannot generate them.

### `plot_render_preview`

Render the same declarative JSON scene to a black-on-white line-art PNG.

- This is a **debug/preview** tool, enabled only when the skill config has
  `debug: true`. With `debug=false` it returns `success=false` and writes
  nothing; plot with `plot_draw_scene` or `plot_draw_image` instead (or
  `plot_draw_prompt` when image generation is enabled).
- `debug` is a **skill config**, not a per-call argument. With `debug=true`
  the PNG is written to `output_dir` and `image_path` points to it; the file
  carries orientation/scale metadata that `plot_draw_image` understands.
- `output_name`, `overwrite`, `pixel_to_mm`, `line_width_mm`, and
  `supersample` tune the preview. An empty `output_name` selects the fixed
  slot `last_preview.png` and overwrites it; an explicit name keeps the
  normal overwrite rules.
- `plot_draw_scene` writes the same `last_preview.png` before moving when
  `debug=true`, so this tool is optional. Call it first when the preview
  should exist before planning the draw, then pass `skip_preview=true`.
- Preview failures never block drawing; `plot_draw_scene` reports them as
  warnings and continues.

### `plot_draw_image`

Trace a PNG/JPEG/BMP/WebP file inside the allowed roots and draw it.

- `image_path` must resolve inside `image_dir` or `output_dir`; absolute paths
  outside those roots, `..`, symlink escapes, and unknown suffixes are
  rejected.
- `trace` selects the tracer (config `trace` provides the default):
  - `outline`: ink boundary via marching squares — silhouettes/filled art;
  - `skeleton`: ink centreline via Zhang-Suen thinning — line art/logos;
  - `lineart`: difference-of-Gaussians edges then skeleton — photos/greyscale;
  - `auto`: `skeleton` for preview PNGs carrying Robonix metadata, otherwise
    `outline`; `auto` never selects `lineart`.
- `dry_run=true` plans and validates without moving the machine.
- `orientation`: `auto` (default) honours preview metadata (screen upright,
  1:1 mm scale) and draws metadata-less PNGs screen-upright too; `screen`
  forces upright; `paper` uses the raw raster mapping. `flip_y` mirrors the
  screen orientation when `auto` is used on a file without metadata.
  Rotation requires pre-rotating the file.
- `scale`: `< 0` auto-fits to paper, `0` uses metadata scale, `> 0` is an
  explicit millimetres-per-pixel factor.
- `flip_x`, `flip_y`, `invert`, `threshold`, `simplify_px`, `fill`,
  `margin_mm` adjust tracing. `workspace` can override the configured
  rectangle and pen heights for this call.
- The primitive HOME routine runs before the trajectory unless
  `skip_home=true`; `auto_home=true` runs it again after a successful draw.

### `plot_list_images`

List drawable files under `image_dir` / `output_dir` (direct children only,
not recursive), optionally filtered by a filename substring. Use it instead of
guessing file paths.

## Motion parameters and homing

Speed, acceleration, deceleration, jerk, and the primitive's pen-height
profile are **not** skill config and are never sent by this skill. To change
them, call the primitive tools directly:

- `plotter_set_params` — change the global profile (0 = keep current).
- `plotter_home` — home the machine on request.

Because this skill never calls SET_PARAM, a profile selected by the user
persists across every draw. `home_before_draw` (config, default true) homes
before each trajectory; `skip_home=true` suppresses it when the planner
already called `plotter_home`. `auto_home` (config or per-call) performs
homing after a successful job. Neither changes the profile.

## Coordinates

- Units: millimetres; paper origin at the lower-left, +X right, +Y up.
- Drawable area and pen heights are reported by the `geometry` contract on
  activation; this skill hardcodes none of them.
- The skill validates every waypoint against the workspace before moving; an
  out-of-bounds plan returns `success=false` with no motion.