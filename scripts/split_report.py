"""Measure the core / developer-tooling split: sizes, and every import from the core side into the tooling side.

    python scripts/split_report.py            # print the report
    python scripts/split_report.py --check    # exit 1 if the core imports the tooling anywhere but a listed seam

Before the move the tooling still lives inside `tuvl`, so it is told apart by the prefix list below (the table in
PLAN_2.1: "Moves to tuvl_insight"). After the move the tooling is the `tuvl_insight` package and the same check
runs with no list at all. The edges are read from the source with `ast`, so nothing is imported or executed.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# Command modules that stay in the core (production CLI).
CORE_COMMANDS = {"run", "runs", "db", "doctor", "keys", "memory", "storage", "iam"}

TOOLING_PREFIXES = (
    "tuvl.api.dev",
    "tuvl.api.code_assist",
    "tuvl.core.dev",
    "tuvl_insight.codegen",
    "tuvl_insight.coding_agents",
    "tuvl_insight.review",
    "tuvl_insight.spec",
    "tuvl_insight.specgen",
    "tuvl_insight.taskrun",
    "tuvl_insight.tui",
    "tuvl_insight.uispec",
    "tuvl_insight.audit",
    "tuvl_insight.devmcp",
    "tuvl_insight.devtelemetry",
    "tuvl_insight.gitflow",
    "tuvl_insight.gitutil",
    "tuvl_insight.shipgate",
    "tuvl_insight.shiparchive",
    "tuvl_insight.skills",
    "tuvl_insight.testing",
    "tuvl_insight.validate",
    "tuvl.lock",
    "tuvl_insight.cli.nest",
    "tuvl_insight.cli.laya",
    "tuvl_insight.cli.agent_run",
    "tuvl_insight.cli.session",
    "tuvl_insight.cli.select",
    "tuvl_insight.cli.json_output",
    "tuvl.doctor.toolchain_groups",
)

# Stay in the core even though they sit under a tooling prefix: the dev-key gate and the live-state routes.
CORE_EXCEPTIONS = (
    "tuvl.core.dev.middleware",
    "tuvl.core.dev.project",
    "tuvl.core.dev.artifacts",
    "tuvl.core.dev.extensions",
    "tuvl.api.dev.breakpoints",
    "tuvl.api.dev.memory",
    "tuvl.api.dev.schedules",
    "tuvl.api.dev.storage",
    "tuvl.api.dev.judges",
)


def is_tooling(module: str) -> bool:
    if any(module == p or module.startswith(p + ".") for p in CORE_EXCEPTIONS):
        return False
    if module.startswith("tuvl.cli.commands."):
        return module.split(".")[3] not in CORE_COMMANDS
    if module.startswith("tuvl.cli.") and module.rsplit(".", 1)[-1].startswith("ship_"):
        return True
    return any(module == p or module.startswith(p + ".") for p in TOOLING_PREFIXES)


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def absolute(module: str, path: Path, node: ast.ImportFrom) -> str | None:
    """The dotted module a `from ... import` names, resolving a relative import against *module*."""
    if node.level == 0:
        return node.module
    package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, *([node.module] if node.module else [])])


def scan() -> tuple[dict[str, int], dict[str, dict[str, bool]]]:
    """Return {module: line count} and {module: {imported module: only imported lazily}}."""
    sizes: dict[str, int] = {}
    files: dict[str, Path] = {}
    for path in sorted((SRC / "tuvl").rglob("*.py")):
        sizes[module_name(path)] = len(path.read_text().splitlines())
        files[module_name(path)] = path
    edges: dict[str, dict[str, bool]] = {}
    for module, path in files.items():
        tree = ast.parse(path.read_text())
        inside_function: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                inside_function.update(id(inner) for inner in ast.walk(node))
        found: dict[str, bool] = {}
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                base = absolute(module, files[module], node)
                if base and base.split(".")[0] == "tuvl":
                    targets = [
                        f"{base}.{a.name}" if f"{base}.{a.name}" in sizes else base
                        for a in node.names
                    ]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names if a.name.split(".")[0] == "tuvl"]
            for target in targets:
                if target in sizes and target != module:
                    lazy = id(node) in inside_function
                    found[target] = found.get(target, True) and lazy
        edges[module] = found
    return sizes, edges


# Modules that may import the tooling: the places where the engine reaches for it on purpose.
SEAMS = {
    "tuvl.cli": "registers the one-word toolchain commands",
    "tuvl.cli.verbs": "registers the verb tree; each toolchain action is imported when it runs",
    "tuvl.core.dev.extensions": "mounts the tooling's /dev routes in dev mode",
    "tuvl.doctor": "registers the toolchain's check groups",
}


def seam_violations() -> list[tuple[str, str]]:
    """Core modules that import the tooling without being a listed seam."""
    sizes, edges = scan()
    tooling = {m for m in sizes if is_tooling(m)}
    return sorted(
        (module, target)
        for module, targets in edges.items()
        if module not in tooling and module not in SEAMS
        for target in targets
        if target in tooling
    )


def core_used_by_tooling() -> list[str]:
    """The core modules the tooling imports: the surface the tooling depends on."""
    sizes, edges = scan()
    tooling = {m for m in sizes if is_tooling(m)}
    return sorted({t for m in tooling for t in edges[m] if t not in tooling})


def main() -> int:
    sizes, edges = scan()
    tooling = {m for m in sizes if is_tooling(m)}
    core = set(sizes) - tooling
    print(
        f"modules   core {len(core):4d} ({sum(sizes[m] for m in core):6d} lines)   "
        f"tooling {len(tooling):4d} ({sum(sizes[m] for m in tooling):6d} lines)"
    )
    cut = defaultdict(list)
    for module in sorted(core):
        for target, lazy in sorted(edges[module].items()):
            if target in tooling:
                cut[module].append((target, lazy))
    total = sum(len(v) for v in cut.values())
    print(f"\nimports from core into tooling: {total} in {len(cut)} modules")
    for module, targets in cut.items():
        for target, lazy in targets:
            print(f"  {'lazy' if lazy else 'TOP '}  {module}  ->  {target}")
    back = sum(1 for m in tooling for t in edges[m] if t in core)
    print(
        f"\nimports from tooling into core: {back} (allowed; kept to a short list by tests/test_boundary.py)"
    )
    if "--check" in sys.argv and (bad := seam_violations()):
        for module, target in bad:
            print(f"  not a seam: {module} -> {target}", file=sys.stderr)
        print("\nsplit check FAILED: the core imports the tooling", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
