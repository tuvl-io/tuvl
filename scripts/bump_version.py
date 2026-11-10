#!/usr/bin/env python3
"""Set or check the release version in every file of this repository that carries it.

    python scripts/bump_version.py 2.0.1          # write the version, then run `uv lock`
    python scripts/bump_version.py 2.0.1 --no-lock
    python scripts/bump_version.py --check        # exit 1 when the files disagree

tuvl and tuvl-insight release in lockstep, so both packages, the `tuvl-insight` pin of the
`standard` extra, the `tuvl` pin of tuvl-insight and the fallback pin in `tuvl init` all carry the
same version. The npm client and the docs live in other repositories; see CONTRIBUTING.md.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# PEP 440 release, optionally with a pre-release (a, b, rc) or post/dev segment.
VERSION = re.compile(r"^\d+(\.\d+){1,3}((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


@dataclass(frozen=True)
class Site:
    label: str
    path: str
    pattern: re.Pattern[str]  # group 1 is the version


SITES: tuple[Site, ...] = (
    Site("tuvl version", "pyproject.toml", re.compile(r'^version = "([^"]+)"', re.M)),
    Site(
        "tuvl-insight pin (standard extra)",
        "pyproject.toml",
        re.compile(r'"tuvl-insight==([0-9][^"]*)"'),
    ),
    Site(
        "tuvl-insight version",
        "insight/pyproject.toml",
        re.compile(r'^version = "([^"]+)"', re.M),
    ),
    Site(
        "tuvl pin (tuvl-insight)",
        "insight/pyproject.toml",
        re.compile(r'"tuvl==([0-9][^"]*)"'),
    ),
    Site(
        "tuvl init fallback pin",
        "insight/src/tuvl_insight/cli/commands/init.py",
        re.compile(r'"tuvl\[standard\]>=([0-9][^"]*)"'),
    ),
)


def read(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for site in SITES:
        match = site.pattern.search((root / site.path).read_text())
        if match is None:
            raise SystemExit(f"{site.path}: could not find the {site.label}")
        found[site.label] = match.group(1)
    return found


def check(root: Path) -> list[str]:
    """One line per disagreement; empty when every site carries the same version."""
    found = read(root)
    if len(set(found.values())) == 1:
        return []
    return [f"{label}: {version}" for label, version in found.items()]


def write(root: Path, version: str) -> None:
    if not VERSION.match(version):
        raise SystemExit(f"'{version}' is not a PEP 440 version such as 2.0.1 or 2.1.0b1")
    for site in SITES:
        path = root / site.path
        text = path.read_text()
        replaced, n = site.pattern.subn(
            lambda m: m.group(0).replace(m.group(1), version), text, count=1
        )
        if n != 1:
            raise SystemExit(f"{site.path}: could not find the {site.label}")
        path.write_text(replaced)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("version", nargs="?", help="the new version, e.g. 2.0.1")
    parser.add_argument("--check", action="store_true", help="verify the files agree and exit")
    parser.add_argument("--no-lock", action="store_true", help="do not run `uv lock` afterwards")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    if args.check or args.version is None:
        problems = check(args.root)
        if problems:
            print("version files disagree:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        print(f"version files agree: {next(iter(read(args.root).values()))}")
        return 0

    write(args.root, args.version)
    print(f"set {args.version} in {len(SITES)} places")
    if not args.no_lock:
        subprocess.run(["uv", "lock"], cwd=args.root, check=True)  # noqa: S603, S607
    print("next: update CHANGELOG.md, commit, tag v" + args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
