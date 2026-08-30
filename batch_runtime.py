from __future__ import annotations

from pathlib import Path


def select_requested_media(
    media_files: list[Path],
    input_root: Path,
    requested_paths: object,
) -> list[Path]:
    """Limit a batch to relative paths selected by the UI retry action."""
    if not requested_paths or isinstance(requested_paths, (str, bytes)):
        return media_files
    requested = {
        str(path).replace("/", "\\").casefold()
        for path in requested_paths
        if str(path).strip()
    }
    if not requested:
        return media_files
    return [
        path
        for path in media_files
        if str(path.relative_to(input_root)).replace("/", "\\").casefold() in requested
    ]
