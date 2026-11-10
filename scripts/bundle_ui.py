"""
scripts/bundle_ui.py — Compile and package the Tuvl Insight React SPA.

Runs `pnpm run build` in the ui/ directory, then copies the compiled output
into insight/src/tuvl_insight/static/ so it is included in the tuvl-insight wheel.

This script is run by maintainers before publishing a new release:
    make build-ui

The compiled output (insight/src/tuvl_insight/static/) is gitignored — it is never
committed to the repository. It is built fresh by this script at package
time (locally via `make build`/`make smoke`, or in CI in
.github/workflows/publish.yml) and only then bundled as static files inside
the wheel. Skipping this step produces an installable but broken wheel with
no UI assets (the empty-wheel bug from 2026.2.3b1).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
UI_DIR = ROOT / "ui"
TARGET_DIR = ROOT / "insight" / "src" / "tuvl_insight" / "static"


def main() -> None:
    if not UI_DIR.is_dir():
        print(f"ERROR: ui/ directory not found at {UI_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"→ Installing frontend dependencies (pnpm install in {UI_DIR})...")
    result = subprocess.run(
        ["pnpm", "install", "--frozen-lockfile"],
        cwd=UI_DIR,
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: pnpm install failed.", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"→ Building Tuvl Insight frontend (pnpm run build in {UI_DIR})...")
    result = subprocess.run(
        ["pnpm", "run", "build"],
        cwd=UI_DIR,
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: pnpm run build failed.", file=sys.stderr)
        sys.exit(result.returncode)

    dist_dir = UI_DIR / "dist"
    if not dist_dir.is_dir():
        print(
            f"ERROR: Expected build output at {dist_dir} but it was not created.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Clear existing static payload before copying fresh build output.
    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist_dir, TARGET_DIR, dirs_exist_ok=True)
    print(f"✓ Frontend assets copied to {TARGET_DIR}")

    print("  Ready to publish: run `make build` to produce both wheels.")


if __name__ == "__main__":
    main()
