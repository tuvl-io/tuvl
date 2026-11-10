"""Move modules from `tuvl` to `tuvl_insight` and rewrite every import that names them.

    python scripts/rewrite_imports.py leaf --dry-run     # show what would move
    python scripts/rewrite_imports.py leaf                # git mv, then rewrite imports

A group is a list of (old, new) dotted module paths (see GROUPS). The script

1. rewrites `import` / `from ... import` statements and whole-string dotted paths ("tuvl.spec.ids" in a
   monkeypatch target or `importlib.resources.files("tuvl.skills")`) in every Python file under src/, insight/,
   tests/ and scripts/, using `ast`, so only real references change and prose is left alone;
2. `git mv`s the files, creating the `__init__.py` of any new parent package.

It is a one-off tool for the split; each group is one commit of renames plus this generated rewrite.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_SRC = ROOT / "src" / "tuvl"
NEW_SRC = ROOT / "insight" / "src" / "tuvl_insight"
SCAN = ("src", "insight/src", "tests", "scripts")

#: the lock file is a file name, not a module path
LOCK_FILE = "tuvl.lock"

#: command modules that stay in the engine (the production CLI)
CORE_COMMANDS = {"run", "runs", "db", "doctor", "keys", "memory", "storage"}

LEAF = [
    "spec", "specgen", "taskrun", "codegen", "coding_agents", "skills", "review", "audit", "uispec", "tui",
    "devmcp", "devtelemetry", "testing", "gitflow", "gitutil", "shipgate", "shiparchive",
]  # fmt: skip


def _same(names: list[str]) -> list[tuple[str, str]]:
    return [(f"tuvl.{n}", f"tuvl_insight.{n}") for n in names]


def _cli_commands() -> list[tuple[str, str]]:
    out = []
    for path in sorted((OLD_SRC / "cli" / "commands").glob("*.py")):
        if path.stem != "__init__" and path.stem not in CORE_COMMANDS:
            out.append((f"tuvl.cli.commands.{path.stem}", f"tuvl_insight.cli.commands.{path.stem}"))
    return out


GROUPS: dict[str, list[tuple[str, str]]] = {
    "leaf": _same(LEAF),
    "lint": _same(["validate", "lock"]),
    "cli": _cli_commands() + _same(["cli.nest", "cli.laya", "cli.agent_run", "cli.session", "cli.select", "cli.json_output"]),
    "api": [
        ("tuvl_insight.api.code_assist", "tuvl_insight.api.code_assist"),
        *[
            (f"tuvl.api.dev.{n}", f"tuvl_insight.api.dev.{n}")
            for n in ("files", "specs", "jobs", "agents", "validation", "telemetry", "canvas", "probe")
        ],
        ("tuvl_insight.api.dev_core_router", "tuvl_insight.api.dev_core_router"),
    ],
    "doctor": [("tuvl.doctor.toolchain_groups", "tuvl_insight.doctor_groups")],
}  # fmt: skip


def make_mapper(pairs: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
    ordered = sorted(pairs, key=lambda p: -len(p[0]))

    def mapped(name: str) -> str | None:
        for old, new in ordered:
            if name == old or name.startswith(old + "."):
                return new + name[len(old) :]
        return None

    return mapped


def rewrite_source(text: str, mapped) -> str:  # type: ignore[no-untyped-def]
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, int, int, str]] = []  # (line, col, end_line, end_col, replacement)

    def span(node: ast.AST) -> tuple[int, int, int, int]:
        return (
            node.lineno,
            node.col_offset,
            node.end_lineno or node.lineno,
            node.end_col_offset or 0,
        )  # type: ignore[attr-defined]

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            groups: dict[str, list[ast.alias]] = {}
            changed = False
            for alias in node.names:
                as_module = mapped(f"{node.module}.{alias.name}")
                if alias.name != "*" and as_module is not None:
                    parent, _, leaf = as_module.rpartition(".")
                    groups.setdefault(parent, []).append(ast.alias(name=leaf, asname=alias.asname))
                    changed = True
                else:
                    target = mapped(node.module) or node.module
                    changed = changed or target != node.module
                    groups.setdefault(target, []).append(alias)
            if changed:
                indent = " " * node.col_offset
                text_out = "".join(
                    f"{indent}from {mod} import "
                    + ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in names)
                    + "\n"
                    for mod, names in groups.items()
                )
                edits.append((*span(node), text_out[len(indent) :].rstrip("\n")))
        elif isinstance(node, ast.Import):
            new_names = []
            changed = False
            for alias in node.names:
                target = mapped(alias.name)
                changed = changed or target is not None
                new_names.append((target or alias.name, alias.asname))
            if changed:
                body = ", ".join(n + (f" as {a}" if a else "") for n, a in new_names)
                edits.append((*span(node), f"import {body}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            target = (
                mapped(value)
                if value.startswith("tuvl.")
                and value != LOCK_FILE
                and value.replace(".", "").replace("_", "").isalnum()
                else None
            )
            if target is not None and "\n" not in value:
                quote = lines[node.lineno - 1][node.col_offset]
                edits.append((*span(node), f"{quote}{target}{quote}"))

    for line, col, end_line, end_col, replacement in sorted(edits, reverse=True):
        first, last = lines[line - 1], lines[end_line - 1]
        lines[line - 1 : end_line] = [first[:col] + replacement + last[end_col:]]
    return "".join(lines)


def python_files() -> list[Path]:
    files: list[Path] = []
    for top in SCAN:
        files += [
            p
            for p in (ROOT / top).rglob("*.py")
            if "__pycache__" not in p.parts and ".venv" not in p.parts
        ]
    return files


def target_path(new: str) -> Path:
    return NEW_SRC.joinpath(*new.split(".")[1:])


def source_path(old: str) -> Path:
    base = OLD_SRC.joinpath(*old.split(".")[1:])
    return base if base.is_dir() else base.with_suffix(".py")


def ensure_packages(directory: Path) -> None:
    while directory != NEW_SRC.parent and not (directory / "__init__.py").exists():
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text(
            '"""Developer tooling for tuvl."""\n' if directory == NEW_SRC else ""
        )
        subprocess.run(["git", "add", str(directory / "__init__.py")], cwd=ROOT, check=True)
        directory = directory.parent


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in GROUPS:
        print(f"usage: rewrite_imports.py <{'|'.join(GROUPS)}> [--dry-run]", file=sys.stderr)
        return 2
    pairs = GROUPS[sys.argv[1]]
    dry = "--dry-run" in sys.argv
    mapped = make_mapper(pairs)

    for old, new in pairs:
        src = source_path(old)
        assert src.exists(), f"{old}: {src} does not exist"
        print(
            f"  {src.relative_to(ROOT)}  ->  {target_path(new).relative_to(ROOT)}{'/' if src.is_dir() else '.py'}"
        )
    if dry:
        return 0

    changed = 0
    for path in python_files():
        text = path.read_text()
        if "tuvl" not in text:
            continue
        new_text = rewrite_source(text, mapped)
        if new_text != text:
            path.write_text(new_text)
            changed += 1
    print(f"rewrote imports in {changed} files")

    for old, new in pairs:
        src = source_path(old)
        dest = target_path(new) if src.is_dir() else target_path(new).with_suffix(".py")
        ensure_packages(dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "mv", str(src), str(dest)], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
