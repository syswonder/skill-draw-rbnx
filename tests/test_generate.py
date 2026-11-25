# SPDX-License-Identifier: MIT
"""Scene validation, deterministic PNG rendering, and metadata tests."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PIL")

from draw.generate import (  # noqa: E402
    GenerateError,
    GenerateOptions,
    generate_scene,
    parse_scene,
)
from draw.metadata import read_png_hints  # noqa: E402


def _opts(**overrides) -> GenerateOptions:
    values = dict(
        default_width_mm=20.0,
        default_height_mm=10.0,
        max_width_mm=20.0,
        max_height_mm=10.0,
        pixel_to_mm=0.1,  # 10 px/mm -> 200 x 100 px
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
        "shapes": [
            {"type": "circle", "cx": 15, "cy": 5, "r": 2},
            {"type": "rect", "x": 1, "y": 6, "w": 3, "h": 2},
        ],
    }
    values.update(overrides)
    return values


def test_parse_scene_expands_strokes_and_shapes():
    strokes, width_mm, height_mm = parse_scene(json.dumps(_scene()), _opts())

    assert width_mm == 20.0
    assert height_mm == 10.0
    assert len(strokes) == 3
    assert [len(stroke.points) for stroke in strokes] == [3, 128, 4]
    assert strokes[2].closed is True


def test_parse_scene_accepts_dict_points_and_arcs():
    scene = {
        "strokes": [{"points": [{"x": 1, "y": 1}, {"x": 2, "y": 2}]}],
        "shapes": [
            {
                "type": "arc",
                "cx": 10,
                "cy": 5,
                "r": 3,
                "start_deg": 0,
                "end_deg": 90,
            }
        ],
    }
    strokes, _w, _h = parse_scene(json.dumps(scene), _opts())
    assert len(strokes) == 2
    assert len(strokes[1].points) > 5


@pytest.mark.parametrize(
    "spec, match",
    [
        ("", "empty"),
        ("{", "not valid JSON"),
        (json.dumps({"strokes": [[[1, 1], [2, 2]]], "width_mm": 21}), "width_mm"),
        (json.dumps({"strokes": [[[1, 1], [21, 2]]]}), "outside"),
        (json.dumps({"strokes": []}), "at least one"),
        (json.dumps({"strokes": [[[1, 1], [2, 2]]], "width_mm": -1}), "width_mm"),
        (
            json.dumps(
                {"strokes": [[{"x": True, "y": 1}, [2, 2]]]}
            ),
            "boolean",
        ),
        (
            json.dumps(
                {
                    "strokes": [
                        {"points": [[1, 1], [2, 2]], "width_mm": 20}
                    ]
                }
            ),
            "width_mm",
        ),
    ],
)
def test_invalid_scenes_are_rejected(spec, match):
    with pytest.raises(GenerateError, match=match):
        parse_scene(spec, _opts())


def test_limits_are_enforced():
    two_strokes = json.dumps(
        {"strokes": [[[1, 1], [2, 2]], [[3, 3], [4, 4]]]}
    )
    with pytest.raises(GenerateError, match="more than 1 strokes"):
        parse_scene(two_strokes, _opts(max_strokes=1))

    long_spec = json.dumps(_scene())
    with pytest.raises(GenerateError, match="too large"):
        parse_scene(long_spec, _opts(max_spec_bytes=10))

    many_points = json.dumps(
        {"strokes": [[[float(i % 10), float(i % 5)] for i in range(50)]]}
    )
    with pytest.raises(GenerateError, match="more than 10 points"):
        parse_scene(many_points, _opts(max_points=10))


def test_generate_scene_writes_png_and_metadata(tmp_path):
    output = tmp_path / "nested" / "scene.png"
    generated = generate_scene(json.dumps(_scene()), output, _opts())

    assert output.is_file()
    assert (generated.width_px, generated.height_px) == (200, 100)
    assert generated.width_mm == 20.0
    assert generated.height_mm == 10.0
    assert generated.pixel_to_mm == pytest.approx(0.1)
    assert generated.stroke_count == 3
    assert generated.orientation == "screen"

    hints = read_png_hints(str(output))
    assert hints.orientation == "screen"
    assert hints.pixel_to_mm == pytest.approx(0.1)
    assert hints.width_mm == pytest.approx(20.0)
    assert hints.height_mm == pytest.approx(10.0)
    assert hints.offset_x_mm == 0.0
    assert hints.offset_y_mm == 0.0

    from PIL import Image

    with Image.open(output) as image:
        assert image.size == (200, 100)
        assert image.mode == "L"
        assert image.getextrema()[0] < 128  # ink was actually drawn


def test_generate_scene_write_false_validates_without_touching_disk(tmp_path):
    output = tmp_path / "nested" / "scene.png"
    generated = generate_scene(
        json.dumps(_scene()), output, _opts(), write=False
    )

    assert generated.path is None
    assert (generated.width_px, generated.height_px) == (200, 100)
    assert generated.width_mm == 20.0
    assert generated.height_mm == 10.0
    assert generated.pixel_to_mm == pytest.approx(0.1)
    assert generated.stroke_count == 3
    assert generated.point_count == 3 + 128 + 4
    assert not output.exists()
    assert not output.parent.exists()


def test_generate_scene_requires_output_when_writing():
    with pytest.raises(GenerateError, match="output path"):
        generate_scene(json.dumps(_scene()), None, _opts())


def test_generated_png_is_white_with_black_ink(tmp_path):
    output = tmp_path / "line.png"
    generate_scene(
        json.dumps(
            {
                "width_mm": 20,
                "height_mm": 10,
                "shapes": [
                    {"type": "line", "x1": 2, "y1": 5, "x2": 18, "y2": 5}
                ],
            }
        ),
        output,
        _opts(),
    )

    from PIL import Image

    with Image.open(output) as image:
        pixels = image.load()
        assert pixels[1, 1] == 255
        # The horizontal line is at y=5 mm -> row 50 in a 100 px image.
        assert min(pixels[x, 50] for x in range(20, 180)) < 128


def test_too_many_pixels_is_rejected(tmp_path):
    with pytest.raises(GenerateError, match="max_pixels"):
        generate_scene(
            json.dumps(_scene()), tmp_path / "big.png", _opts(max_pixels=50)
        )


def test_invalid_pixel_to_mm_is_rejected(tmp_path):
    with pytest.raises(GenerateError, match="pixel_to_mm"):
        generate_scene(
            json.dumps(_scene()), tmp_path / "bad.png", _opts(pixel_to_mm=0.0)
        )