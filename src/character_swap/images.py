from __future__ import annotations

import base64
import hashlib
import os
import platform
import shutil
import subprocess
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def encode_b64(path: Path) -> str:
    with path.open("rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def media_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def open_in_viewer(path: Path) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        elif system == "Linux":
            subprocess.run(["xdg-open", str(path)], check=False)
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


def find_first_image(directory: Path) -> Path | None:
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        matches = sorted(directory.glob(ext))
        if matches:
            return matches[0]
    return None


def list_character_images(directory: Path) -> list[Path]:
    items: list[Path] = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        items.extend(directory.glob(ext))
    return sorted(items)
