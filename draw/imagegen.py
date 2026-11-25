# SPDX-License-Identifier: MIT
"""OpenAI-compatible text-to-image client used by ``prompt``.

The draw skill does not own a generative model. When ``imagegen_enabled`` is
true, this module POSTs to an OpenAI-compatible ``/images/generations``
endpoint, decodes the returned base64 image (or downloads its URL), and
normalises it to an RGB PNG. The resulting file is then traced with the
existing ``lineart`` / ``skeleton`` / ``outline`` pipeline and plotted.

Only ``urllib`` (stdlib) and Pillow are used, matching the package's
dependency footprint. Failures raise :class:`ImageGenError` so the MCP handler
can return ``success=false`` instead of crashing.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, PngImagePlugin, UnidentifiedImageError

from .metadata import ORIENTATION_KEY

#: Refuse absurdly large responses / downloads before decoding them.
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_MAX_ERROR_CHARS = 400


class ImageGenError(RuntimeError):
    """The image service is disabled, misconfigured, unreachable, or failed."""


@dataclass(frozen=True)
class ImageGenOptions:
    """Connection settings for one OpenAI-compatible image request."""

    base_url: str
    model: str
    api_key: str = ""
    size: str = "1024x1024"
    timeout_s: float = 120.0


def endpoint(base_url: str) -> str:
    """Return the images/generations URL for an OpenAI-compatible base URL.

    Accepted forms:
      * ``http://host:8000/v1`` -> ``.../v1/images/generations``
      * ``http://host:8000`` -> ``.../v1/images/generations``
      * a full ``.../images/generations`` URL is used as-is
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ImageGenError("imagegen_base_url is empty")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ImageGenError(
            "imagegen_base_url must start with http:// or https://"
        )
    if base.endswith("/images/generations"):
        return base
    if base.endswith("/v1"):
        return f"{base}/images/generations"
    return f"{base}/v1/images/generations"


def build_payload(
    prompt: str, options: ImageGenOptions, *, size: str = ""
) -> dict:
    """Build the OpenAI-compatible request body."""
    text = (prompt or "").strip()
    if not text:
        raise ImageGenError("prompt is empty")
    if not (options.model or "").strip():
        raise ImageGenError("imagegen_model is empty")
    payload = {
        "model": options.model.strip(),
        "prompt": text,
        "n": 1,
    }
    chosen = (size or options.size or "").strip()
    if chosen:
        payload["size"] = chosen
    return payload


def generate_image(
    prompt: str, options: ImageGenOptions, *, size: str = ""
) -> bytes:
    """Call the configured service and return the raw image bytes."""
    payload = build_payload(prompt, options, size=size)
    data = _post_json(endpoint(options.base_url), payload, options)
    return extract_image_bytes(data, options)


def extract_image_bytes(
    payload: dict, options: ImageGenOptions
) -> bytes:
    """Decode ``b64_json``/``image`` fields or download the first ``url``.

    Supports the common OpenAI shape (``data[0].b64_json`` or ``data[0].url``)
    plus the ``images: [base64, ...]`` shape used by several local gateways.
    """
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            for key in ("b64_json", "image"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return _decode_base64(value)
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                return _download(url.strip(), options)

    images = payload.get("images")
    if isinstance(images, list) and images:
        return _decode_base64(images[0])

    image = payload.get("image")
    if isinstance(image, str) and image.strip():
        return _decode_base64(image)

    raise ImageGenError(
        "image service returned no image"
        + _error_suffix(payload)
    )


def write_png(
    raw: bytes, path: Path, *, max_pixels: int = 0
) -> tuple[int, int]:
    """Normalise raw image bytes to an RGB PNG and atomically write *path*.

    Transparency is composited over white so the line-art tracer sees ink on
    a light background. The PNG is tagged ``robonix_orientation=screen`` so a
    later ``image`` call draws it upright instead of mirroring the raw
    raster. Returns the image dimensions.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageGenError(
            "image service returned data that is not a decodable image"
        ) from exc

    width, height = image.size
    if width <= 0 or height <= 0:
        raise ImageGenError(f"generated image has invalid size {width}x{height}")
    if max_pixels > 0 and (width > max_pixels or height > max_pixels):
        raise ImageGenError(
            f"generated image {width}x{height} exceeds "
            f"generate_max_pixels={max_pixels}"
        )

    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        image = canvas
    elif image.mode != "RGB":
        image = image.convert("RGB")

    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text(ORIENTATION_KEY, "screen")
    try:
        image.save(tmp, format="PNG", pnginfo=pnginfo)
        os.replace(tmp, path)
    except (OSError, ValueError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ImageGenError(f"cannot write generated image {path}: {exc}") from exc
    return width, height


# ── HTTP helpers ─────────────────────────────────────────────────────
def _post_json(url: str, payload: dict, options: ImageGenOptions) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if (options.api_key or "").strip():
        headers["Authorization"] = f"Bearer {options.api_key.strip()}"
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST"
    )
    raw = _read_url(request, options.timeout_s, context="image service")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageGenError("image service returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ImageGenError("image service returned an unexpected JSON value")
    return parsed


def _download(url: str, options: ImageGenOptions) -> bytes:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ImageGenError("generated image URL must start with http:// or https://")
    request = urllib.request.Request(url, method="GET")
    return _read_url(request, options.timeout_s, context="generated image URL")


def _read_url(request, timeout_s: float, *, context: str) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(_MAX_ERROR_CHARS).decode("utf-8", "replace").strip()
        raise ImageGenError(
            f"{context} returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageGenError(f"cannot reach {context}: {exc}") from exc
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ImageGenError(f"{context} response is too large")
    return raw


def _decode_base64(value: str) -> bytes:
    text = str(value or "").strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    # Some services wrap base64 payloads across lines; whitespace is not
    # data, so drop it before strict validation.
    text = "".join(text.split())
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError("image service returned invalid base64 image data") from exc


def _error_suffix(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return f": {message.strip()[:_MAX_ERROR_CHARS]}"
    if isinstance(error, str) and error.strip():
        return f": {error.strip()[:_MAX_ERROR_CHARS]}"
    return ""