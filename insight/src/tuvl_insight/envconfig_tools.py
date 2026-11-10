"""The environment inventory: what a project's YAML and Python read, merged with what it declares.

The engine's side (``kind: EnvConfig``, the engine catalog, the boot check) is :mod:`tuvl.core.envconfig`. This
is the tooling's: it scans the project for ``${VAR}`` references and ``os.environ`` reads, and :func:`inventory`
merges them with the catalog and the manifest. Insight shows the result, ``tuvl ship`` writes ``.env.example`` and
the Helm values from it, and ``tuvl lint project`` holds a project with a manifest to it.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl.core.envconfig import (
    ENGINE_CATALOG,
    ENV_CONFIG_KIND,
    VARIABLE_NAME,
    EnvVar,
    dotenv_path,
    load_manifest,
    read_dotenv,
    yaml_files,
)

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:[^}]*)?\}")


_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)", re.IGNORECASE)


#: kinds whose documents are development material or describe the variables themselves
_SCAN_SKIP_KINDS = frozenset({ENV_CONFIG_KIND, "Test", "TaskPlan", "Idea", "IdeaIndex", "test"})


_PY_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "tests", "specs", "deploy", "dist"}
)


@dataclass
class Reference:
    name: str
    #: every reference names a default (`${VAR:default}`), so the variable may stay unset
    all_have_default: bool = True
    files: list[str] = field(default_factory=list)

    def add(self, file: str, has_default: bool) -> None:
        self.all_have_default = self.all_have_default and has_default
        if file not in self.files:
            self.files.append(file)


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _python_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _PY_SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _env_reads(tree: ast.AST) -> Iterator[str]:
    """Names read with a literal key: ``os.environ["X"]``, ``os.environ.get("X")``, ``os.getenv("X")``."""
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            key = node.slice
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("get", "getenv")
                and (
                    _is_environ(func.value)
                    or (func.attr == "getenv" and _is_name(func.value, "os"))
                )
            ):
                key = node.args[0]
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and VARIABLE_NAME.match(key.value)
        ):
            yield key.value


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and _is_name(node.value, "os")


def scan_references(project_dir: Path) -> dict[str, Reference]:
    """Every environment variable the project's YAML and Python refer to, with where."""
    root = Path(project_dir).resolve()
    found: dict[str, Reference] = {}

    def note(name: str, rel: str, has_default: bool) -> None:
        found.setdefault(name, Reference(name)).add(rel, has_default)

    for path in yaml_files(root):
        try:
            documents = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(root))
        for document in documents:
            if isinstance(document, dict) and document.get("kind") in _SCAN_SKIP_KINDS:
                continue
            for text in _strings(document):
                for match in _REF.finditer(text):
                    note(match.group(1), rel, match.group(2) is not None)
    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, OSError, UnicodeDecodeError, ValueError):
            continue
        rel = str(path.relative_to(root))
        for name in _env_reads(tree):
            # a literal read without a default is a requirement; with one it cannot be told apart here
            note(name, rel, True)
    return found


@dataclass
class EnvEntry:
    var: EnvVar
    #: "engine" (the catalog), "project" (an EnvConfig), or "referenced" (found in the project only)
    source: str
    referenced_by: list[str]
    is_set: bool
    #: the value of a non-secret variable when set; never a secret's
    value: str | None = None
    #: a `${VAR}` with no default points at it, so the project cannot run without it
    needed: bool = False


def inventory(
    project_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    manifest: Mapping[str, EnvVar] | None = None,
) -> list[EnvEntry]:
    """Engine catalog, project declarations and project references, merged by variable name.

    The declarations are read from the project's files unless *manifest* is given.
    """
    root = Path(project_dir).resolve()
    manifest = load_manifest(root) if manifest is None else manifest
    environ = os.environ if environ is None else environ
    dotenv = read_dotenv(root)
    refs = scan_references(root)

    entries: list[EnvEntry] = []
    names = list(
        dict.fromkeys(
            [*manifest, *(n for n in refs if n not in manifest and n not in ENGINE_CATALOG)]
        )
    )
    # engine variables appear when the project refers to one or has set one
    names += [
        n for n in ENGINE_CATALOG if n not in names and (n in refs or n in environ or n in dotenv)
    ]
    for name in names:
        if name in manifest:
            var, source = manifest[name], "project"
        elif name in ENGINE_CATALOG:
            var, source = ENGINE_CATALOG[name], "engine"
        else:
            var = EnvVar(name, secret=bool(_SECRET_NAME.search(name)))
            source = "referenced"
        raw = environ.get(name, dotenv.get(name))
        ref = refs.get(name)
        entries.append(
            EnvEntry(
                var=var,
                source=source,
                referenced_by=ref.files if ref else [],
                is_set=raw is not None and raw != "",
                value=None if var.secret or not raw else raw,
                needed=bool(ref and not ref.all_have_default),
            )
        )
    return entries


def undeclared_references(project_dir: Path) -> dict[str, Reference]:
    """References to variables neither an ``EnvConfig`` nor the engine catalog declares."""
    manifest = load_manifest(project_dir)
    return {
        name: ref
        for name, ref in scan_references(project_dir).items()
        if name not in manifest and name not in ENGINE_CATALOG
    }


def unused_declarations(project_dir: Path) -> list[str]:
    """Manifest variables nothing in the project refers to (the engine reads its own)."""
    refs = scan_references(project_dir)
    return [
        name
        for name in load_manifest(project_dir)
        if name not in refs and name not in ENGINE_CATALOG
    ]


def ship_variables(project_dir: Path) -> tuple[list[EnvEntry], list[EnvEntry]]:
    """``(secrets, plain)`` entries the deployment must supply, for the Secret and the Helm values.

    Secrets are the declared secrets and the referenced variables that look like one, plus the
    engine's signing key and database password. Plain ones are those with a declared default or an
    example; they go in the chart values.
    """
    root = Path(project_dir)
    entries = {e.var.name: e for e in inventory(root, environ={})}
    for name in ("TUVL_SIGNING_KEY", "POSTGRES_PASSWORD"):
        entries.setdefault(name, EnvEntry(ENGINE_CATALOG[name], "engine", [], False))
    secrets = [e for e in entries.values() if e.var.secret]
    plain = [
        e
        for e in entries.values()
        if not e.var.secret and e.source == "project" and e.var.default is not None
    ]
    return secrets, plain


def gitignored(project_dir: Path) -> bool:
    """Whether the project's `.gitignore` lists `.env` (a plain line check, not git itself)."""
    path = Path(project_dir) / ".gitignore"
    try:
        lines = {ln.strip() for ln in path.read_text().splitlines()}
    except OSError:
        return False
    return bool(lines & {".env", "/.env", ".env*", "*.env", ".env.*"}) or any(
        ln.startswith(".env") and not ln.startswith(".env.") for ln in lines
    )


def write_dotenv(project_dir: Path, updates: Mapping[str, str | None]) -> None:
    """Set (a string) or remove (``None``) variables in the project's `.env`, keeping the rest.

    The file is created owner-readable only; the project's other lines and comments are untouched.
    """
    from dotenv import set_key, unset_key

    path = dotenv_path(project_dir)
    if not path.exists():
        path.touch(mode=0o600)
    for name, value in updates.items():
        if not VARIABLE_NAME.match(name):
            raise ValueError(f"'{name}' is not an environment variable name")
        if value is None:
            unset_key(path, name, quote_mode="never")
        else:
            if "\n" in value or "\r" in value:
                raise ValueError(f"the value of {name} must be one line")
            set_key(path, name, value, quote_mode="auto")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
