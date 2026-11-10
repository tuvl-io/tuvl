"""What a project needs installed, decided before the image is built.

``tuvl ship`` installs ``tuvl`` plus only the extras the project uses (see
:mod:`tuvl.extras`) and refuses an image whose code agents import a package the
project does not declare. Both come from static reads — the project's YAML and
the AST of ``agents/*.py`` — so the same project always yields the same image
and nothing is resolved at run time.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import re
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl.extras import EXTRAS

_SKIP_DIRS = {".venv", "venv", "node_modules", "deploy", "__pycache__", ".git", ".tuvl"}
_VECTOR_BUILTINS = {"tuvl.data_search", "tuvl.data_ingest"}
_VECTOR_KINDS = {"EmbeddingRegistry", "EmbeddingConfig", "CollectionRegistry", "CollectionConfig"}
_LABEL = re.compile(r'^LABEL io\.tuvl\.extras="([^"]*)"', re.MULTILINE)


@dataclass
class Needs:
    #: extra → why it is needed ("workflows/x.yaml: agent fetch uses mcp").
    extras: dict[str, list[str]] = field(default_factory=dict)
    #: (file, module) imported by a code agent but declared nowhere.
    undeclared: list[tuple[str, str]] = field(default_factory=list)
    #: (file, module) that cannot be mapped to a package (not installed here).
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    #: declared dependencies no code agent imports.
    unused: list[str] = field(default_factory=list)

    def add(self, extra: str, reason: str) -> None:
        self.extras.setdefault(extra, []).append(reason)

    @property
    def names(self) -> list[str]:
        return sorted(self.extras)


def _yaml_docs(project_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    docs = []
    for path in sorted(project_dir.rglob("*.y*ml")):
        rel = path.relative_to(project_dir)
        if path.suffix not in {".yaml", ".yml"} or _SKIP_DIRS & set(rel.parts):
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get("kind"):
            docs.append((rel, data))
    return docs


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _yaml_needs(project_dir: Path, needs: Needs) -> None:
    for rel, doc in _yaml_docs(project_dir):
        kind = doc.get("kind")
        spec = doc.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        if kind == "DataSource" and spec.get("type") == "redis":
            needs.add("redis", f"{rel}: a redis datasource")
        if kind in _VECTOR_KINDS:
            needs.add("vector", f"{rel}: {kind}")
        if kind != "Workflow":
            continue
        trigger = spec.get("trigger")
        mcp = trigger.get("mcp") if isinstance(trigger, dict) else None
        if isinstance(mcp, dict) and mcp.get("enabled"):
            needs.add("mcp", f"{rel}: the workflow is exported as an MCP tool")
        for node in _walk(spec.get("agents") or []):
            if node.get("use") == "mcp" or ("mcp" in node and "allow" in node):
                needs.add("mcp", f"{rel}: agent {node.get('id') or '?'} calls an MCP server")
            code = node.get("code")
            if isinstance(code, dict) and code.get("run") in _VECTOR_BUILTINS:
                needs.add("vector", f"{rel}: {code['run']}")


def _telemetry_needs(project_dir: Path, needs: Needs) -> None:
    path = project_dir / ".tuvl" / "telemetry.yaml"
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return
    if isinstance(data, dict) and data.get("otlp_protocol") == "grpc":
        needs.add("otel-grpc", ".tuvl/telemetry.yaml: otlp_protocol is grpc")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared(project_dir: Path) -> set[str]:
    try:
        meta = tomllib.loads((project_dir / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    names = set()
    for spec in meta.get("project", {}).get("dependencies", []):
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _tuvl_requirements() -> set[str]:
    """Packages ``tuvl`` itself guarantees (its unconditional dependencies)."""
    try:
        requires = md.requires("tuvl") or []
    except md.PackageNotFoundError:
        return set()
    names = set()
    for spec in requires:
        if "extra ==" in spec:
            continue
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _local_modules(project_dir: Path) -> set[str]:
    local = {p.name for p in project_dir.iterdir() if p.is_dir()}
    local |= {p.stem for p in project_dir.glob("*.py")}
    return local | {p.stem for p in (project_dir / "agents").glob("*.py")}


def _imports(agents_dir: Path, project_dir: Path) -> dict[str, list[str]]:
    """Top-level module → files that import it (relative imports excluded)."""
    found: dict[str, list[str]] = {}
    for path in sorted(agents_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(project_dir))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if rel not in found.setdefault(top, []):
                    found[top].append(rel)
    return found


def _import_needs(project_dir: Path, needs: Needs) -> None:
    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        return
    declared = _declared(project_dir)
    guaranteed = _tuvl_requirements()
    providers = md.packages_distributions()
    local = _local_modules(project_dir)
    by_module = {extra.module: name for name, extra in EXTRAS.items() if extra.shipped}
    used: set[str] = set()
    for module, files in _imports(agents_dir, project_dir).items():
        if module in sys.stdlib_module_names or module == "tuvl" or module in local:
            continue
        if module in by_module:
            needs.add(by_module[module], f"{files[0]}: imports {module}")
            continue
        dists = {_normalize(d) for d in providers.get(module, [])}
        if not dists:
            if _normalize(module) in declared:
                used.add(_normalize(module))
            else:
                needs.unresolved.extend((f, module) for f in files)
            continue
        used |= dists
        if not dists & (declared | guaranteed):
            needs.undeclared.extend((f, module) for f in files)
    needs.unused = sorted(declared - used - {"tuvl"})


def detect(project_dir: Path, forced: tuple[str, ...] = ()) -> Needs:
    needs = Needs()
    _yaml_needs(project_dir, needs)
    _telemetry_needs(project_dir, needs)
    _import_needs(project_dir, needs)
    for extra in forced:
        needs.add(extra, "--extra")
    return needs


def recorded_extras(dockerfile: Path) -> set[str] | None:
    """Extras a previously generated Dockerfile installs; None if it predates the label."""
    try:
        match = _LABEL.search(dockerfile.read_text())
    except OSError:
        return None
    if not match:
        return None
    return {e for e in match.group(1).split(",") if e}
