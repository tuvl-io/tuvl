"""Copying text out of the terminal UI, where the mouse cannot select it."""

from __future__ import annotations

import shutil
import subprocess

_TOOLS = (("pbcopy",), ("wl-copy",), ("xclip", "-selection", "clipboard"), ("clip",))


def copy(text: str) -> bool:
    """Put *text* on the system clipboard; False when this machine has no tool for it."""
    for argv in _TOOLS:
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text.encode(), check=True, timeout=5)  # noqa: S603
            except (OSError, subprocess.SubprocessError):
                continue
            return True
    return False
