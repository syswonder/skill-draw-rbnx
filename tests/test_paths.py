# SPDX-License-Identifier: MIT
"""Directory policy tests for render_preview / image / list_images."""

from __future__ import annotations

from pathlib import Path

import pytest

from draw.paths import ImagePathError, ImageStore


def _store(tmp_path: Path, **kwargs) -> tuple[ImageStore, Path]:
    root = tmp_path / "images"
    root.mkdir()
    return ImageStore(str(root), **kwargs), root


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-a-real-image")
    return path


def test_relative_and_absolute_inputs_resolve_inside_the_root(tmp_path):
    store, root = _store(tmp_path)
    image = _touch(root / "cat.png")

    assert store.resolve_input("cat.png") == image.resolve()
    assert store.resolve_input(str(image)) == image.resolve()


def test_input_outside_the_read_roots_is_rejected(tmp_path):
    store, _root = _store(tmp_path)
    outside = _touch(tmp_path / "secret.png")

    with pytest.raises(ImagePathError, match="must be inside"):
        store.resolve_input(str(outside))
    with pytest.raises(ImagePathError, match="must be inside"):
        store.resolve_input("../secret.png")


def test_unsupported_suffix_and_missing_file_are_rejected(tmp_path):
    store, root = _store(tmp_path)
    _touch(root / "notes.txt")

    with pytest.raises(ImagePathError, match="unsupported image type"):
        store.resolve_input("notes.txt")
    with pytest.raises(ImagePathError, match="not found"):
        store.resolve_input("missing.png")


def test_symlink_escape_is_rejected(tmp_path):
    store, root = _store(tmp_path)
    outside = _touch(tmp_path / "outside.png")
    link = root / "link.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    with pytest.raises(ImagePathError, match="must be inside"):
        store.resolve_input("link.png")


def test_output_names_are_sanitised_and_forced_to_png(tmp_path):
    store, root = _store(tmp_path)

    nested = store.resolve_output("../../evil name.jpg")
    assert nested.parent == root.resolve()
    assert nested.name == "evil_name.jpg.png"

    generated = store.resolve_output("")
    assert generated.parent == root.resolve()
    assert generated.name.startswith("draw_")
    assert generated.suffix == ".png"


def test_output_overwrite_requires_the_flag(tmp_path):
    store, root = _store(tmp_path)
    target = _touch(root / "plot.png")

    with pytest.raises(ImagePathError, match="already exists"):
        store.resolve_output("plot.png")
    assert store.resolve_output("plot.png", overwrite=True) == target.resolve()


def test_preview_defaults_to_last_preview_and_always_overwrites(tmp_path):
    store, root = _store(tmp_path)
    preview = store.resolve_preview()
    assert preview.name == "last_preview.png"

    _touch(preview)
    assert store.resolve_preview() == preview


def test_preview_with_explicit_name_keeps_overwrite_rules(tmp_path):
    store, root = _store(tmp_path)
    target = _touch(root / "custom.png")

    with pytest.raises(ImagePathError, match="already exists"):
        store.resolve_preview("custom.png")
    assert store.resolve_preview("custom.png", overwrite=True) == target.resolve()


def test_output_dir_is_readable_and_listed(tmp_path):
    store = ImageStore(str(tmp_path / "images"), str(tmp_path / "out"))
    generated = _touch(tmp_path / "out" / "made.png")
    _touch(tmp_path / "images" / "input.png")

    assert store.resolve_input(str(generated)) == generated.resolve()
    assert store.resolve_input("made.png") == generated.resolve()
    names = [path.name for path in store.list_images()]
    assert names == ["input.png", "made.png"]


def test_list_images_filters_and_ignores_nested_files(tmp_path):
    store, root = _store(tmp_path)
    _touch(root / "alpha.png")
    _touch(root / "beta.PNG")
    _touch(root / "notes.txt")
    _touch(root / "nested" / "gamma.png")

    names = [path.name for path in store.list_images()]
    assert names == ["alpha.png", "beta.PNG"]
    assert [path.name for path in store.list_images("ALP")] == ["alpha.png"]
    assert store.list_images("zzz") == []


def test_list_images_respects_the_cap(tmp_path):
    store, root = _store(tmp_path, max_list_images=2)
    for name in ("a.png", "b.png", "c.png"):
        _touch(root / name)

    assert len(store.list_images()) == 2


def test_relative_roots_use_robonix_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    images = workspace / "pics"
    images.mkdir(parents=True)
    monkeypatch.setenv("ROBONIX_WORKSPACE", str(workspace))

    store = ImageStore("pics")
    assert store.image_dir == images.resolve()
    assert store.output_dir == images.resolve()


def test_default_image_dir_is_workspace_images(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ROBONIX_WORKSPACE", str(workspace))

    store = ImageStore()
    assert store.image_dir == (workspace / "images").resolve()
    assert store.output_dir == store.image_dir