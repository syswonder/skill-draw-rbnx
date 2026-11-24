# skill-draw-rbnx

Robonix **skill** for declarative scene drawing, prompt-to-image drawing,
debug previews, and traced image drawing. It owns
`robonix/skill/plot/*` and turns JSON scenes, generated PNGs, or existing
images into pen-up/down waypoints for the plotter primitive.

Catalog name: `robonix.skill.draw`.

The skill never talks TCP or PLC. It resolves the plotter primitive through
Atlas and reuses the repository [`SDK/`](../../../SDK) for image geometry;
`skeleton` and `lineart` tracing live in [`xyz_draw/trace.py`](xyz_draw/trace.py).
Text writing lives in [`skill-text-rbnx`](../skill-text-rbnx).

## Capability surface

| Contract | Mode | Transport | Source / handler |
| --- | --- | --- | --- |
| `robonix/skill/plot/driver` | rpc | gRPC | `Driver(CMD_INIT, config_json)` — lifecycle gate |
| `robonix/skill/plot/config` | rpc + MCP | MCP | `plot_config() → debug, imagegen_enabled, imagegen_ready, output_dir, preview_name, home_before_draw`; read-only |
| `robonix/skill/plot/draw_scene` | rpc + MCP | MCP | `draw_scene(spec, offset_x, offset_y, dry_run, auto_home, skip_home, skip_preview, workspace)` |
| `robonix/skill/plot/draw_prompt` | rpc + MCP | MCP | `draw_prompt(prompt, output_name, overwrite, trace, size, flip, dry_run, auto_home, skip_home, workspace)` |
| `robonix/skill/plot/render_preview` | rpc + MCP | MCP | `render_preview(spec, output_name, overwrite, pixel_to_mm, line_width_mm, supersample)` |
| `robonix/skill/plot/draw_image` | rpc + MCP | MCP | `draw_image(image_path, orientation, trace, scale, offsets, thresholds, dry_run, auto_home, skip_home, workspace)` |
| `robonix/skill/plot/list_images` | rpc + MCP | MCP | `list_images(filter)` |

Every trajectory tool validates the complete plan before moving.
Out-of-workspace points, an empty plan, or more than `max_points` waypoints
return `success=false` with no motion. `dry_run=true` returns counts and
bounds without moving.

## Drawing policy

Call `plot_config` before planning a drawing request. It exposes the runtime
switches that the planner cannot otherwise see:

| Flag | Meaning |
| --- | --- |
| `debug` | `render_preview` is enabled, and a non-dry-run `draw_scene` writes `output_dir/last_preview.png` before moving |
| `imagegen_enabled` | `draw_prompt` is enabled in config |
| `imagegen_ready` | `draw_prompt` is enabled **and** has a base URL and model |
| `home_before_draw` | draw tools run the primitive HOME routine before the trajectory |
| `preview_name` | fixed preview filename (`last_preview.png`) |

Routing:

| Request | Path |
| --- | --- |
| Structured/geometric (shapes, coordinates, JSON) | `plotter_home` → `render_preview` (only if `debug=true`) → `draw_scene` |
| Free-form/illustrative and `imagegen_ready=true` | `plotter_home` → `draw_prompt`; on failure, author a simple declarative scene and continue with `draw_scene` |

Pass `skip_home=true` after `plotter_home` and `skip_preview=true` after
`render_preview`. The skill still enforces home/preview when the flags are
missing, so the plan remains advisory rather than a hard requirement.
Preview failures are warnings and never block drawing.

## `draw_scene`

The primary tool for structured drawings. The scene is never rasterised for
plotting and is never contour-traced: every stroke/shape becomes exactly one
pen-down polyline, so a line is drawn once and circles/arcs keep their
analytic geometry. `width_mm` is ignored for plotting because the pen tip
defines the line width.

```json
{
  "width_mm": 180,
  "height_mm": 150,
  "strokes": [
    [[10, 10], [20, 20], [30, 10]],
    {"points": [[40, 40], [50, 50]], "width_mm": 0.5, "closed": false}
  ],
  "shapes": [
    {"type": "line",     "x1": 10, "y1": 10, "x2": 90, "y2": 10},
    {"type": "polyline", "points": [[10, 20], [20, 30]], "closed": false},
    {"type": "polygon",  "points": [[10, 20], [20, 30], [30, 20]]},
    {"type": "rect",     "x": 10, "y": 10, "w": 40, "h": 20},
    {"type": "circle",   "cx": 90, "cy": 75, "r": 30},
    {"type": "ellipse",  "cx": 90, "cy": 75, "rx": 40, "ry": 20},
    {"type": "arc",      "cx": 90, "cy": 75, "r": 30,
     "start_deg": 0, "end_deg": 180}
  ]
}
```

- Canvas coordinates are millimetres; origin at the lower-left, +X right,
  +Y up. `offset_x` / `offset_y` translate the canvas inside the workspace.
- `strokes` are drawn first, then `shapes`, in list order; each may set
  `closed`.
- Only line art: no fills, colours, gradients, or text.
- `width_mm` / `height_mm` default to the config and must fit the workspace.
- `dry_run=true` never homes and never writes a preview.
- Unless `skip_home=true`, HOME runs before the trajectory;
  `home_before_draw: false` disables it globally. `auto_home` homes again
  after a successful draw.
- With `debug=true`, a non-dry-run draw also renders the same scene to
  `output_dir/last_preview.png` before moving, unless `skip_preview=true`.
  Preview failures are reported in `message` and never block drawing.
- Scene size is bounded by `max_points`, `generate_max_strokes`, and
  `generate_max_points`.

## `render_preview`

Renders the same JSON scene to a black-on-white line-art PNG.

- Enabled only when the config has `debug: true`. With `debug: false` it
  returns `success=false` and writes nothing.
- An empty `output_name` selects the fixed slot `last_preview.png` and
  overwrites it; an explicit name keeps the normal `overwrite` rules.
- The PNG is written only inside `output_dir` and carries orientation/scale
  metadata that `draw_image` understands.
- `draw_scene` writes the same `last_preview.png` automatically when
  `debug=true`, so this tool is optional. Call it first when the preview
  must exist before the draw is planned, then pass `skip_preview=true`.
- It does not move the machine, so there is no `dry_run`; validation is the
  same as for `draw_scene`.

## `draw_prompt`

Generates an image from a text prompt and draws it. This is the preferred
path for free-form/illustrative requests when `imagegen_ready=true`; use
`draw_scene` when the request specifies exact geometry or JSON.

```text
prompt
  → POST {imagegen_base_url}/images/generations   (OpenAI-compatible)
  → decode b64_json or download the returned URL
  → normalise to RGB PNG, write it into output_dir
  → draw_image(trace=lineart) → waypoints → primitive
```

- **Disabled by default.** Requires `imagegen_enabled: true`, a non-empty
  `imagegen_base_url`, and a model. Disabled or misconfigured calls return
  `success=false` without any network request. Check `imagegen_ready` first.
- Every successful generation writes the PNG into `output_dir` **before**
  tracing. If tracing or drawing later fails, the file remains.
- `trace` defaults to `lineart`; `skeleton` and `outline` can be requested.
- Generated PNGs are tagged screen-upright and drawn that way by default;
  `flip=true` selects the raw paper-raster direction (vertically mirrored).
- `dry_run=true` still generates the PNG and plans waypoints, but does not
  move the machine.
- On failure, fall back to `draw_scene` with a simple scene authored from the
  request; do not retry the same prompt.
- HOME runs before the trajectory unless `skip_home=true`; `auto_home` homes
  after a successful draw.
- `imagegen_prompt_suffix` appends style words to every prompt, for example
  `"high contrast, clean white background, black line art, no text"`.

This is a separate text-to-image service, not the Pilot's VLM. The VLM can
read images but cannot generate them.

## `draw_image`

Traces a PNG/JPEG/BMP/WebP file inside the allowed roots and draws it.

```text
image
  → path validation (image_dir / output_dir only)
  → grayscale → binarise (Otsu, explicit threshold, or DoG for lineart)
  → trace according to `trace`
  → fit_to_paper (aspect-preserving) or explicit scale + offset
  → absolute (x, y, z) waypoints
  → plan validation → trajectory
```

| `trace` | Algorithm | Best for |
| --- | --- | --- |
| `outline` | SDK marching squares on the ink boundary, then Douglas-Peucker | silhouettes and filled shapes; line art becomes two parallel sides |
| `skeleton` | Zhang-Suen thinning to the ink centreline, then Douglas-Peucker | line art, logos, handwriting; one pen stroke per line |
| `lineart` | Difference-of-Gaussians edges with a hysteresis threshold, then thinning | photos and grey artwork; produces line art instead of filled blobs |
| `""` / `auto` | `skeleton` for PNGs with Robonix metadata, otherwise `outline` | backward-compatible default; never auto-selects `lineart` |

`lineart` sensitivity is controlled by `trace_lineart_sigma` and
`trace_lineart_detail` (strong threshold = mean + detail × standard
deviation; lower keeps more edges). `trace_spur_px` removes short spurs,
`trace_min_length_mm` drops short isolated polylines, and `trace_bridge_px`
joins nearby endpoints into one stroke. If output is dashed, raise
`trace_bridge_px` (3 → 6) or lower `trace_lineart_detail`.

Unless `skip_home=true`, HOME runs before the trajectory. `auto_home` homes
after a successful draw. `dry_run=true` validates and plans without moving.

Prefer `draw_scene` for scenes. `render_preview → draw_image` still pays for
rasterisation and thinning even though `trace=auto` selects `skeleton`.

## `list_images`

Lists drawable files directly under `image_dir` and `output_dir` (not
recursive), optionally filtered by a filename substring. Use it before
`draw_image` instead of guessing paths.

## `plot_config`

Read-only. Returns the policy flags above plus `image_dir`, `output_dir`,
`preview_name`, and `home_before_draw`. It never moves the machine and never
writes files.

## File roots and path policy

- `image_dir` defaults to `images/` under the deployment root;
  `output_dir` defaults to the same directory.
- Both are read roots. `output_dir` is the only write root: previews and
  generated images are written there.
- Relative paths resolve against `ROBONIX_WORKSPACE` when set, otherwise the
  deployment root (the directory containing `packages/`).
- Absolute paths are accepted only when they resolve inside a read root.
  `..`, symlink escapes, unknown suffixes, and missing files are rejected.
- `list_images` lists direct children only.

## Orientation and scale

`draw_image` does not understand image content; it traces pixels.

| Input | Default behavior |
| --- | --- |
| PNG produced by `render_preview` / `draw_scene` | `robonix_orientation=screen`; drawn upright at the stored millimetre scale; `trace=auto` selects `skeleton` |
| Third-party PNG, no metadata | drawn screen-upright (as displayed), auto-fitted to paper |
| Third-party PNG, with metadata | metadata orientation and scale are honoured |

`orientation` values:

- `""` / `"auto"`: metadata first; otherwise screen-upright and auto-fit.
- `"screen"`: force screen-upright (`flip_y=true`).
- `"paper"`: force the raw raster mapping (`flip_y=false`).

`scale` values:

- `< 0`: force auto-fit, ignoring metadata scale.
- `= 0`: use metadata scale when present, otherwise auto-fit.
- `> 0`: explicit millimetres per pixel; `offset_x` / `offset_y` become the
  placement origin in millimetres.

`flip_x` always works independently. `flip_y` mirrors the screen orientation
only in `orientation=auto` on a file without metadata. Rotation is not a
parameter: rotate the file beforehand.

Preview PNG `tEXt` metadata:

| Key | Meaning |
| --- | --- |
| `robonix_orientation` | `screen` (currently always) |
| `robonix_pixel_to_mm` | millimetres per pixel |
| `robonix_width_mm` / `robonix_height_mm` | canvas size |
| `robonix_offset_x_mm` / `robonix_offset_y_mm` | canvas offset in the workspace |

## Boot ordering

This skill must come **after** `primitive-hdu-cartesian-plotter-rbnx` in the deployment
manifest. Activation resolves `robonix/primitive/plotter/{trajectory,home}`;
if they are missing it returns `Deferred` and retries on the next MCP call.

## Driver-init lifecycle

`scripts/start.sh` execs `python3 -m xyz_draw.main`.

1. `CMD_INIT`: parse and validate config, image roots, and preview settings;
   no primitive connection.
2. `CMD_ACTIVATE`: resolve the two primitive contracts through Atlas.
3. `CMD_DEACTIVATE` / `CMD_SHUTDOWN`: close the gRPC channels.

## Layout

```text
skill-draw-rbnx/
├── package_manifest.yaml
├── capabilities/
│   ├── driver.v1.toml
│   ├── config.v1.toml
│   ├── render_preview.v1.toml
│   ├── draw_scene.v1.toml
│   ├── draw_prompt.v1.toml
│   ├── draw_image.v1.toml
│   ├── list_images.v1.toml
│   ├── lib/plot/{srv,msg}/...          # Config, Draw*, GenerateImage, ListImages, Workspace
│   └── primitive/plotter/...           # mirrored, consume-only contracts
├── xyz_draw/
│   ├── main.py                         # MCP tools + lifecycle
│   ├── scene.py                        # declarative scene → waypoints
│   ├── generate.py                     # preview renderer + scene parser
│   ├── image.py                        # PNG → waypoints (outline/skeleton/lineart)
│   ├── imagegen.py                     # OpenAI-compatible image client
│   ├── trace.py                        # skeleton / line-art algorithms
│   ├── metadata.py                     # PNG orientation/scale hints
│   ├── paths.py                        # image roots and preview naming
│   ├── plan.py                         # workspace validation and bounds
│   ├── primitive_client.py             # Atlas discovery + gRPC calls
│   └── geometry/                       # vendored image + trajectory (image, trajectory)
├── scripts/{build,start}.sh
├── tests/                              # pure-unit tests, no runtime needed
├── config.spec
└── CAPABILITY.md
```

The mirrored `capabilities/primitive/plotter/*` files exist only so
`rbnx codegen --mcp` emits the stubs this skill calls. They are **not**
listed in `package_manifest.yaml`, so Atlas does not treat the skill as a
provider.

## Config

Deployment config lives in the `xyz_draw` skill `config` block of
[`robonix_manifest.yaml`](../../../robonix_manifest.yaml). Full defaults and
constraints are in [`config.spec`](config.spec).

```yaml
# Drawable area and pen heights are NOT configured here. They are read
# from the primitive's `geometry` contract on activation, so this skill
# carries no machine-specific numbers and works on any sized rig.
image_dir: images          # read root (deployment-root relative)
output_dir: ""             # empty -> same as image_dir; the only write root
debug: true                # checked-in deployment value; config.spec default false
home_before_draw: true     # HOME before a trajectory
auto_home: true            # HOME after a successful draw
imagegen_enabled: false
imagegen_base_url: ""      # OpenAI-compatible, e.g. http://127.0.0.1:8000/v1
imagegen_model: ""
imagegen_api_key: ""       # or IMAGE_GEN_API_KEY / OPENAI_API_KEY
imagegen_size: "1024x768"
imagegen_timeout_s: 120.0
imagegen_prompt_suffix: ""
trace: auto                # auto / outline / skeleton / lineart
simplify_px: 1.5
image_margin_mm: 5.0
image_fill: 0.8
trace_lineart_sigma: 1.2
trace_lineart_detail: 1.0
trace_spur_px: 2.0
trace_min_length_mm: 1.0
trace_bridge_px: 3.0
generate_pixel_to_mm: 0.25
generate_line_width_mm: 0.5
generate_supersample: 2
max_points: 50000
```

Per-call MCP fields override the config; `0` / negative sentinels are
documented in each `.srv`. Speed, acceleration, deceleration, and jerk remain
global primitive parameters (`plotter_set_params`) and are never modified by
this skill.

## Build / run standalone

The deployment shares one uv environment at the repository root; this package
does not carry its own `requirements.txt`.

```bash
bash scripts/build.sh                        # uv sync + rbnx codegen
uv run --project ../../.. pytest tests -q
```

`rbnx boot` launches the skill from the manifest. To run it manually:

```bash
ROBONIX_ATLAS=127.0.0.1:50051 bash scripts/start.sh
```

Tests cover path policy, scene validation, preview naming, PNG metadata,
orientation/scale mapping, direct-scene waypoints, skeleton/lineart tracing,
the image-generation client, and trajectory validation. They need neither the
Robonix runtime nor a PLC. End-to-end checks need the primitive, a real PLC,
and — for `draw_prompt` — a reachable image service.

## Verification

```bash
rbnx caps | grep skill/plot
# Expected: xyz_draw provider with
#   robonix/skill/plot/{driver,config,draw_scene,draw_prompt,
#                       render_preview,draw_image,list_images}
```

Suggested checks, in order:

1. `draw_scene` with `dry_run=true` — point count and bounds, no motion.
2. `render_preview` with `debug=true` — `images/last_preview.png` exists.
3. `draw_scene` of a small shape — `last_preview.png` is rewritten before
   motion when `debug=true`.
4. `list_images` — confirms the filenames visible to `draw_image`.
5. `draw_image` with `dry_run=true`, then trace a small file for real.

## Known limits

- Scenes are line art only: no fills, colours, gradients, or text. Use
  `skill-text-rbnx` for text.
- `draw_scene` ignores `width_mm` for plotting; the physical pen defines the
  stroke width.
- `outline` traces the ink boundary, so line art becomes two parallel sides.
- `lineart` extracts edges; it does not reproduce shading or greyscale.
- `skeleton` on filled regions produces a centreline, not the filled shape.
- Rotation is not a parameter; pre-rotate the file.
- `draw_prompt` depends on an external image service and never uses the VLM
  for generation.
- There is no STOP command. A failed job leaves the pen where it stopped;
  home before retrying.

## License

MIT. See [LICENSE](../../../LICENSE).