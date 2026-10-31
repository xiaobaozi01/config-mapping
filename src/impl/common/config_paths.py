"""Locate project-level configuration files from the installed source tree."""

from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def config_path(*parts: str) -> Path:
    """Return a path below the project root's config directory."""
    return (_PROJECT_ROOT / "config").joinpath(*parts)
