from __future__ import annotations

from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    """Find the repository root from a test file or helper module."""
    path = (start or Path(__file__)).resolve()
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError(f"Could not locate project root from {path}")


PROJECT_ROOT = find_project_root()
