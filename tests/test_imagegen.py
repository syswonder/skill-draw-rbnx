# SPDX-License-Identifier: MIT
"""Text-to-image client tests: URL/payload resolution, decoding, PNG output."""

from __future__ import annotations

import base64
import io

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from draw import imagegen  # noqa: E402
from draw.imagegen import (  # noqa: E402
    ImageGenError,
    ImageGenOptions,
    build_payload,
    endpoint,
    extract_image_bytes,
    generate_image,
    write_png,
)
from draw.metadata import read_png_hints  # noqa: E402


def _options(**overrides) -> ImageGenOptions:
    values = dict(
        base_url="http://127.0.0.1:8000/v1",
        model="test-model",
        api_key="",
        size="512x512",
        timeout_s=5.0,
    )
    values.update(overrides)
    return ImageGenOptions(**values)


def _png_bytes(*, size=(8, 6), mode="RGB", color=(0, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()


# ── endpoint / payload ───────────────────────────────────────────────
def test_endpoint_accepts_v1_and_bare_hosts():
    assert endpoint("http://h:8000/v1") == "http://h:8000/v1/images/generations"
    assert endpoint("http://h:8000/v1/") == "http://h:8000/v1/images/generations"
    assert endpoint("http://h:8000") == "http://h:8000/v1/images/generations"
    assert (
        endpoint("https://api.example.com/v1/images/generations")
        == "https://api.example.com/v1/images/generations"
    )


def test_endpoint_rejects_empty_or_non_http_base_url():
    with pytest.raises(ImageGenError, match="empty"):
        endpoint("")
    with pytest.raises(ImageGenError, match="http"):
        endpoint("ftp://example.com/v1")


def test_payload_uses_default_and_per_call_size():
    options = _options(size="1024x1024")
    assert build_payload("a cat", options) == {
        "model": "test-model",
        "prompt": "a cat",
        "n": 1,
        "size": "1024x1024",
    }
    assert build_payload("a cat", options, size="256x256")["size"] == "256x256"


def test_payload_rejects_empty_prompt_and_model():
    with pytest.raises(ImageGenError, match="prompt is empty"):
        build_payload("   ", _options())
    with pytest.raises(ImageGenError, match="imagegen_model is empty"):
        build_payload("a cat", _options(model=" "))


# ── response decoding ────────────────────────────────────────────────
def test_extract_decodes_openai_b64_json():
    raw = _png_bytes()
    payload = {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]}
    assert extract_image_bytes(payload, _options()) == raw


def test_extract_decodes_data_url_and_local_gateway_shapes():
    raw = _png_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    wrapped = encoded[:8] + "\n" + encoded[8:]
    data_url = "data:image/png;base64," + wrapped
    assert extract_image_bytes({"images": [data_url]}, _options()) == raw
    assert extract_image_bytes({"image": data_url}, _options()) == raw


def test_extract_downloads_url(monkeypatch):
    calls = {}

    def fake_download(url, options):
        calls["url"] = url
        return b"downloaded"

    monkeypatch.setattr(imagegen, "_download", fake_download)
    payload = {"data": [{"url": "https://cdn.example.com/a.png"}]}
    assert extract_image_bytes(payload, _options()) == b"downloaded"
    assert calls["url"] == "https://cdn.example.com/a.png"


def test_extract_reports_service_error_and_missing_image():
    with pytest.raises(ImageGenError, match="no image: quota exceeded"):
        extract_image_bytes({"error": {"message": "quota exceeded"}}, _options())
    with pytest.raises(ImageGenError, match="no image"):
        extract_image_bytes({}, _options())


def test_extract_rejects_invalid_base64():
    with pytest.raises(ImageGenError, match="invalid base64"):
        extract_image_bytes({"data": [{"b64_json": "not base64!"}]}, _options())


# ── PNG writing ──────────────────────────────────────────────────────
def test_write_png_composites_transparency_over_white(tmp_path):
    path = tmp_path / "cat.png"
    width, height = write_png(_png_bytes(mode="RGBA", color=(0, 0, 0, 0)), path)

    assert (width, height) == (8, 6)
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)
    # Tagged so a later image call does not mirror the image.
    assert read_png_hints(str(path)).orientation == "screen"
    assert not (tmp_path / "cat.png.tmp").exists()


def test_write_png_rejects_oversized_and_non_image(tmp_path):
    with pytest.raises(ImageGenError, match="exceeds generate_max_pixels"):
        write_png(_png_bytes(size=(40, 10)), tmp_path / "big.png", max_pixels=32)
    with pytest.raises(ImageGenError, match="not a decodable image"):
        write_png(b"not an image", tmp_path / "bad.png")


# ── end-to-end request shape ─────────────────────────────────────────
def test_generate_image_posts_to_images_endpoint(monkeypatch):
    raw = _png_bytes()
    seen = {}

    def fake_post(url, payload, options):
        seen.update(url=url, payload=payload, options=options)
        return {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]}

    monkeypatch.setattr(imagegen, "_post_json", fake_post)
    options = _options(base_url="http://h:8000/v1", api_key="secret")

    assert generate_image("a cat", options) == raw
    assert seen["url"] == "http://h:8000/v1/images/generations"
    assert seen["payload"]["prompt"] == "a cat"
    assert seen["payload"]["model"] == "test-model"
    assert seen["options"].api_key == "secret"