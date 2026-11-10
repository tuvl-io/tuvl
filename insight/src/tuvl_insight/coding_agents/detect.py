"""Find the coding agents installed on ``PATH``."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from tuvl_insight.coding_agents.registry import AGENTS, AgentSpec


@dataclass(frozen=True)
class Detected:
    spec: AgentSpec
    path: str
    version: str


def _output(argv: list[str]) -> str:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=5, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or done.stderr or "").strip()


def _probe(spec: AgentSpec, search_path: str | None) -> Detected | None:
    for binary in spec.binaries:
        path = shutil.which(binary, path=search_path)
        if not path:
            continue
        version = _output([path, *spec.version_args])
        if spec.identify:
            text = f"{version}\n{_output([path, '--help'])[:2000]}".lower()
            if spec.identify not in text:
                continue
        first = version.splitlines()[0] if version else ""
        return Detected(spec, path, first[:80])
    return None


def detect(search_path: str | None = None) -> list[Detected]:
    """Every known agent found on *search_path* (default: ``PATH``), in registry order."""
    found = (_probe(spec, search_path) for spec in AGENTS)
    return [d for d in found if d is not None]
