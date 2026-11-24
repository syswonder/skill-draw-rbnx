# SPDX-License-Identifier: MIT
"""Resolve image input/output paths inside configured directories.

The draw skill is deliberately boring about files:

  * ``image_dir`` and ``output_dir`` are the only directories it reads;
  * ``output_dir`` is the write root for generated PNGs;
  * names are sanitised and cannot escape those roots;
  * absolute paths are accepted only when they resolve inside a read root.

``output_dir`` defaults to ``image_dir``. Keeping both readable means a
generated PNG can always be passed straight back to ``draw_image``, even
when the two directories differ.

Relative directory names are resolved against ``ROBONIX_WORKSPACE`` when
that variable is set (the executor's sandbox root), otherwise the
deployment root (the repository directory that contains ``packages/``).
The skill process itself runs with the package directory as its cwd, so
using the deployment root keeps the default ``images/`` path stable and
in the place users expect.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

DEFAULT_IMAGE_DIR = "images"
DEFAULT_PREVIEW_NAME = "last_preview.png"
DEFAULT_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ImagePathError(ValueError):
    """An image path is missing, outside its root, or has a bad suffix."""


def _workspace_base() -> Path:
    raw = os.environ.get("ROBONIX_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser()
    # rbnx launches start.sh with cwd = package directory. Anchor relative
    # paths at the deployment root so "images" means <repo>/images, not
    # <repo>/packages/skills/skill-draw-rbnx/images.
    return Path(__file__).resolve().parents[4]


def _resolve_root(raw: str, *, default: str = "") -> Path:
    value = (raw or "").strip() or default
    if not value:
        raise ImagePathError("directory path is empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _workspace_base() / path
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise ImagePathError(f"cannot resolve directory {value!r}: {exc}") from exc


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ImageStore:
    """Controlled image roots: read both roots, normally write ``output_dir``."""

    def __init__(
        self,
        image_dir: str = DEFAULT_IMAGE_DIR,
        output_dir: str = "",
        *,
        allowed_suffixes=DEFAULT_SUFFIXES,
        max_list_images: int = 200,
    ) -> None:
        self.image_dir = _resolve_root(image_dir, default=DEFAULT_IMAGE_DIR)
        self.output_dir = _resolve_root(output_dir, default=str(self.image_dir))
        suffixes = tuple(
            str(s).strip().lower() for s in allowed_suffixes if str(s).strip()
        )
        self.allowed_suffixes = tuple(
            s if s.startswith(".") else f".{s}" for s in suffixes
        ) or DEFAULT_SUFFIXES
        self.max_list_images = int(max_list_images)
        if self.max_list_images < 1:
            raise ImagePathError("max_list_images must be >= 1")

    @property
    def read_dirs(self) -> tuple[Path, ...]:
        if self.output_dir == self.image_dir:
            return (self.image_dir,)
        return (self.image_dir, self.output_dir)

    def ensure_output_dir(self) -> Path:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ImagePathError(
                f"cannot create output_dir {self.output_dir}: {exc}"
            ) from exc
        return self.output_dir

    def resolve_input(self, raw: str) -> Path:
        """Resolve ``raw`` to a readable file inside a read root."""
        value = (raw or "").strip()
        if not value:
            raise ImagePathError("image_path is empty")
        path = Path(value).expanduser()
        if not path.is_absolute():
            # A relative name may refer to a generated file in output_dir
            # even when output_dir differs from image_dir. Search both read
            # roots; fall back to image_dir for the error message.
            for root in self.read_dirs:
                try:
                    candidate = (root / path).resolve(strict=False)
                except OSError:
                    continue
                if (
                    _inside(root, candidate)
                    and candidate.is_file()
                    and candidate.suffix.lower() in self.allowed_suffixes
                ):
                    path = candidate
                    break
            else:
                try:
                    path = (self.image_dir / path).resolve(strict=False)
                except OSError as exc:
                    raise ImagePathError(
                        f"cannot resolve image path {value!r}: {exc}"
                    ) from exc
        else:
            try:
                path = path.resolve(strict=False)
            except OSError as exc:
                raise ImagePathError(
                    f"cannot resolve image path {value!r}: {exc}"
                ) from exc
        if not any(_inside(root, path) for root in self.read_dirs):
            roots = ", ".join(str(root) for root in self.read_dirs)
            raise ImagePathError(
                f"image_path must be inside image_dir/output_dir ({roots}), got {path}"
            )
        if path.suffix.lower() not in self.allowed_suffixes:
            raise ImagePathError(
                f"unsupported image type {path.suffix!r}; allowed: "
                + ", ".join(self.allowed_suffixes)
            )
        if not path.is_file():
            raise ImagePathError(f"image not found: {path}")
        return path

    def resolve_output(self, raw_name: str, *, overwrite: bool = False) -> Path:
        """Resolve a generated PNG name inside ``output_dir``.

        The name is reduced to a single filename (no directories), sanitised
        to ``[A-Za-z0-9._-]``, and forced to a ``.png`` suffix.
        """
        root = self.ensure_output_dir()
        name = _safe_name(raw_name)
        path = (root / name).resolve(strict=False)
        if not _inside(root, path):
            raise ImagePathError(
                f"output_name must stay inside output_dir ({root}), got {path}"
            )
        if path.exists() and not overwrite:
            raise ImagePathError(
                f"output already exists: {path}; pass overwrite=true to replace it"
            )
        return path

    def resolve_preview(self, raw_name: str = "", *, overwrite: bool = False) -> Path:
        """Resolve a preview PNG, defaulting to ``last_preview.png``.

        An empty name selects the fixed preview slot and always overwrites
        it, so repeated debug runs do not fail on the previous image. A
        caller-supplied name keeps the normal overwrite rules.
        """
        name = (raw_name or "").strip()
        return self.resolve_output(
            name or DEFAULT_PREVIEW_NAME,
            overwrite=bool(overwrite) or not name,
        )

    def list_images(self, filt: str = "") -> list[Path]:
        """List image files directly under each read root (non-recursive)."""
        needle = (filt or "").strip().lower()
        found: dict[Path, None] = {}
        for root in self.read_dirs:
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
            except OSError as exc:
                raise ImagePathError(f"cannot list {root}: {exc}") from exc
            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in self.allowed_suffixes:
                    continue
                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if not _inside(root, resolved):
                    continue
                if needle and needle not in entry.name.lower():
                    continue
                found[resolved] = None
                if len(found) >= self.max_list_images:
                    break
            if len(found) >= self.max_list_images:
                break
        return sorted(found, key=lambda p: (p.name.lower(), str(p)))


def _safe_name(raw: str) -> str:
    name = (raw or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return f"draw_{stamp}_{uuid.uuid4().hex[:6]}.png"
    name = _SAFE_NAME.sub("_", name).strip("._")
    if not name:
        name = "draw.png"
    if not name.lower().endswith(".png"):
        name += ".png"
    if len(name) > 120:
        name = name[:116].rstrip("._") + ".png"
    return name