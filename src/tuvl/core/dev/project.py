"""The project directory the dev API works on, shared by the live-state routes and the tooling routes."""

from __future__ import annotations

from pathlib import Path

from tuvl.core.config import settings


def project_dir() -> Path:
    return settings.project_dir
