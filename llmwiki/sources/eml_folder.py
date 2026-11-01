"""A folder of .eml files (tests, manual drops). Files already stored are recognised by Message-ID, so re-runs are safe."""
from __future__ import annotations

from pathlib import Path


def iter_folder(folder: str):
    root = Path(folder)
    if not root.exists():
        raise FileNotFoundError(f"eml folder not found: {folder}")
    for p in sorted(root.rglob("*.eml")):
        yield p.read_bytes(), str(p.relative_to(root)), ""
