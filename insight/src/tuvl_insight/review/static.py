"""Tier 1 review: deterministic checks of a project's Python. No model, no tokens.

Reads each file with :mod:`ast`; for the rules that need the workflow (which signals an agent may
return, whether it may write) it reads the project's ``Workflow`` documents directly. ``ruff`` runs
alongside when it is installed.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from tuvl_insight.review.rules import Finding, finding

_NETWORK = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "socket",
    "http.client",
    "smtplib",
    "ftplib",
    "telnetlib",
    "websockets",
}
_PROCESS_MODULES = {"subprocess", "pty", "multiprocessing"}
_PROCESS_CALLS = {
    "os.system",
    "os.popen",
    "os.execv",
    "os.execl",
    "os.execvp",
    "os.spawnl",
    "os.spawnv",
    "os.fork",
}
_DYNAMIC = {"eval", "exec", "compile", "__import__"}
_UNSAFE_LOAD = {"pickle.loads", "pickle.load", "marshal.loads", "marshal.load", "shelve.open"}
_FS_WRITE_CALLS = {
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.rename",
    "os.replace",
    "os.makedirs",
    "os.mkdir",
    "os.chmod",
    "shutil.rmtree",
    "shutil.copy",
    "shutil.copyfile",
    "shutil.copytree",
    "shutil.move",
    "shutil.copy2",
}
_PATH_WRITE = {
    "write_text",
    "write_bytes",
    "unlink",
    "rmdir",
    "mkdir",
    "rename",
    "replace",
    "touch",
    "chmod",
}
_PATH_READ = {"read_text", "read_bytes"}
_NONDETERMINISTIC = {
    "random.random",
    "random.randint",
    "random.choice",
    "random.choices",
    "random.shuffle",
    "random.sample",
    "random.uniform",
    "random.randrange",
    "random.gauss",
    "uuid.uuid1",
    "uuid.uuid4",
    "time.time",
    "time.monotonic",
    "time.perf_counter",
    "time.sleep",
    "time.time_ns",
    "os.urandom",
    "secrets.token_hex",
    "secrets.token_bytes",
    "secrets.token_urlsafe",
    "secrets.choice",
    "secrets.randbelow",
}
_NOW = {"now", "utcnow", "today"}
_SECRET_NAME = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential)", re.I
)
_SECRET_VALUE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"
)
_URL = re.compile(r"https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MUTATORS = {
    "append",
    "extend",
    "insert",
    "add",
    "update",
    "setdefault",
    "pop",
    "popitem",
    "remove",
    "clear",
    "discard",
    "sort",
}
_PURE_CONSTRUCTORS = {
    "Decimal",
    "frozenset",
    "set",
    "dict",
    "list",
    "tuple",
    "str",
    "int",
    "float",
    "bool",
    "bytes",
    "re.compile",
    "field",
    "Field",
    "TypeVar",
    "NewType",
    "namedtuple",
    "getLogger",
    "logging.getLogger",
    "structlog.get_logger",
    "Path",
    "timedelta",
    "datetime.timedelta",
    "frozendict",
    "dataclass",
    "Literal",
    "Optional",
    "Union",
}
_DEFAULT_DEC = "agent"


@dataclass
class AgentInfo:
    run: str
    workflow: str
    agent: str
    effect: str
    routes: set[str]


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "files": self.files,
            "notes": self.notes,
            "findings": [
                f.to_json() for f in sorted(self.findings, key=lambda f: (f.file, f.line, f.code))
            ],
        }


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _imports(tree: ast.Module) -> dict[str, str]:
    """Local name → the dotted thing it stands for (``rq`` → ``requests``, ``now`` → ``datetime.datetime.now``)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _resolve(dotted: str, names: dict[str, str]) -> str:
    head, _, rest = dotted.partition(".")
    base = names.get(head, head)
    return f"{base}.{rest}" if rest else base


def agents_in_workflows(project: Path) -> dict[str, AgentInfo]:
    """``code.run`` → the agent that runs it, read straight from the Workflow documents."""
    out: dict[str, AgentInfo] = {}
    directory = project / "workflows"
    for path in sorted(directory.rglob("*.y*ml")) if directory.is_dir() else []:
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "Workflow":
                continue
            workflow = str((doc.get("metadata") or {}).get("name", ""))
            for agent in (doc.get("spec") or {}).get("agents") or []:
                if isinstance(agent, dict) and agent.get("engine") == "code":
                    run = str((agent.get("code") or {}).get("run", ""))
                    if run:
                        out[run] = AgentInfo(
                            run,
                            workflow,
                            str(agent.get("id", "")),
                            str(agent.get("effect") or "write"),
                            {str(k) for k in (agent.get("routes") or {})},
                        )
    return out


# ── one file ─────────────────────────────────────────────────────────────────


def _decorated_run(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> str | None:
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call) and _dotted(dec.func).split(".")[-1] == _DEFAULT_DEC:
            if (
                dec.args
                and isinstance(dec.args[0], ast.Constant)
                and isinstance(dec.args[0].value, str)
            ):
                return dec.args[0].value
            return ""
        if _dotted(dec).split(".")[-1] == _DEFAULT_DEC:
            return ""
    return None


def _signals(fn: ast.AST) -> list[tuple[str, int]]:
    """String-literal signals a function returns: ``return "s"``, ``return x, "s"``."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        candidates = [value]
        if isinstance(value, ast.Tuple) and value.elts:
            candidates = [value.elts[-1]]
        for c in candidates:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                out.append((c.value, node.lineno))
    return out


def review_source(
    source: str,
    file: str,
    *,
    agents: dict[str, AgentInfo] | None = None,
    in_agents_dir: bool = True,
) -> list[Finding]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [finding("C001", file, exc.lineno or 1, f"does not parse: {exc.msg}", "error")]
    names = _imports(tree)
    out: list[Finding] = []
    # a string on a line of its own is a docstring or a comment in disguise
    docstring_ids = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }
    mutable_globals = {
        t.id
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
        for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
        if isinstance(t, ast.Name)
        and isinstance(
            n.value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
        )
    }

    # module level
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            out.append(
                finding(
                    "C007",
                    file,
                    node.lineno,
                    f"`{_dotted(node.value.func) or 'call'}(…)` runs when the module is imported",
                )
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            callee = _resolve(_dotted(node.value.func), names)
            plain = callee.split(".")[-1]
            if (
                callee not in _PURE_CONSTRUCTORS
                and plain not in _PURE_CONSTRUCTORS
                and not plain[:1].isupper()
            ):
                out.append(
                    finding(
                        "C007",
                        file,
                        node.lineno,
                        f"`{callee}(…)` is called when the module is imported",
                        "warning",
                    )
                )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for mod in mods:
                top = mod.split(".")[0]
                if mod in _NETWORK or top in _NETWORK:
                    out.append(
                        finding("C003", file, node.lineno, f"imports `{mod}`: use `ctx.http`")
                    )
                if top in _PROCESS_MODULES:
                    out.append(finding("C002", file, node.lineno, f"imports `{mod}`"))
                if top == "dotenv":
                    out.append(finding("C004", file, node.lineno, "imports dotenv"))

    for global_node in ast.walk(tree):
        if isinstance(global_node, ast.Global):
            out.append(
                finding(
                    "C008",
                    file,
                    global_node.lineno,
                    f"`global {', '.join(global_node.names)}` keeps state between runs",
                )
            )
        if isinstance(global_node, ast.Call):
            out += _call_findings(global_node, file, names)
        if isinstance(global_node, ast.Attribute) and _resolve(_dotted(global_node), names) in {
            "os.environ"
        }:
            out.append(finding("C004", file, global_node.lineno, "reads `os.environ`"))
        if (
            isinstance(global_node, ast.Constant)
            and isinstance(global_node.value, str)
            and id(global_node) not in docstring_ids
        ):
            text = global_node.value
            if _SECRET_VALUE.search(text):
                out.append(
                    finding(
                        "C010",
                        file,
                        global_node.lineno,
                        "a string that looks like a key, token or private key",
                    )
                )
            elif in_agents_dir and (url := _URL.search(text)) and len(text) < 300:
                out.append(
                    finding(
                        "C009",
                        file,
                        global_node.lineno,
                        f"hard-coded endpoint `{url.group(0)}`",
                    )
                )
        if (
            isinstance(global_node, ast.Assign)
            and isinstance(global_node.value, ast.Constant)
            and isinstance(global_node.value.value, str)
        ):
            for t in global_node.targets:
                if (
                    isinstance(t, ast.Name)
                    and _SECRET_NAME.search(t.id)
                    and len(global_node.value.value) >= 12
                    and " " not in global_node.value.value
                ):
                    out.append(
                        finding(
                            "C010",
                            file,
                            global_node.lineno,
                            f"`{t.id}` is assigned a literal that looks like a secret",
                        )
                    )
    # mutation of module-level containers inside functions
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        local = {a.arg for a in fn.args.args} | {
            t.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        for stmt in ast.walk(fn):
            target = None
            if (
                isinstance(stmt, ast.Call)
                and isinstance(stmt.func, ast.Attribute)
                and stmt.func.attr in _MUTATORS
            ):
                target = stmt.func.value
            elif isinstance(stmt, (ast.Assign, ast.AugAssign)):
                for t in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    if isinstance(t, ast.Subscript):
                        target = t.value
            if (
                isinstance(target, ast.Name)
                and target.id in mutable_globals
                and target.id not in local
            ):
                out.append(
                    finding(
                        "C008",
                        file,
                        getattr(stmt, "lineno", 0),
                        f"mutates the module-level `{target.id}`",
                        "warning",
                    )
                )

    # agents
    run_names: set[str] = set()
    for fn in (n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))):
        run = _decorated_run(fn)
        if run is None:
            continue
        run_names.add(run)
        params = [a.arg for a in fn.args.args]
        if (
            not isinstance(fn, ast.AsyncFunctionDef)
            or len(params) != 2
            or fn.args.vararg
            or fn.args.kwarg
            or not run
        ):
            out.append(
                finding(
                    "C001",
                    file,
                    fn.lineno,
                    f'`{fn.name}` must be `@agent("run") async def {fn.name}(inp, ctx)`',
                )
            )
        info = (agents or {}).get(run)
        if agents is not None and info is None:
            out.append(finding("C013", file, fn.lineno, f"no `engine: code` agent runs `{run}`"))
        if info is not None:
            routes = info.routes
            for sig, line in _signals(fn):
                if sig not in routes:
                    out.append(
                        finding(
                            "C011",
                            file,
                            line,
                            f"returns `{sig}`, but {info.workflow}.{info.agent} routes only {sorted(routes)}",
                        )
                    )
            if info.effect == "read":
                ctx_name = params[1] if len(params) > 1 else "ctx"
                for call_node in ast.walk(fn):
                    if isinstance(call_node, ast.Call):
                        d = _dotted(call_node.func)
                        if d in {
                            f"{ctx_name}.db.create",
                            f"{ctx_name}.db.update",
                            f"{ctx_name}.db.delete",
                        }:
                            out.append(
                                finding(
                                    "C012",
                                    file,
                                    call_node.lineno,
                                    f"`{d}` in {info.workflow}.{info.agent}, declared `effect: read`",
                                )
                            )
                        if d.startswith(f"{ctx_name}.http.") and d.split(".")[-1] in {
                            "post",
                            "put",
                            "patch",
                            "delete",
                        }:
                            out.append(
                                finding(
                                    "C012",
                                    file,
                                    call_node.lineno,
                                    f"`{d}` in {info.workflow}.{info.agent}, declared `effect: read`",
                                    "warning",
                                )
                            )
    return out


def _call_findings(node: ast.Call, file: str, names: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    raw = _dotted(node.func)
    if not raw:
        return out
    full = _resolve(raw, names)
    last = full.split(".")[-1]
    top = full.split(".")[0]
    if full in _DYNAMIC or raw in _DYNAMIC:
        out.append(finding("C002", file, node.lineno, f"`{raw}(…)` evaluates code"))
    elif full in _PROCESS_CALLS or top in _PROCESS_MODULES:
        out.append(finding("C002", file, node.lineno, f"`{raw}(…)` runs a process"))
    elif full in _UNSAFE_LOAD:
        out.append(finding("C002", file, node.lineno, f"`{raw}(…)` can run code from its input"))
    elif full == "yaml.load" and not any(kw.arg == "Loader" for kw in node.keywords):
        out.append(finding("C002", file, node.lineno, "`yaml.load` without a safe Loader"))
    if top in _NETWORK or full.rsplit(".", 1)[0] in _NETWORK or full.startswith("urllib.request"):
        out.append(
            finding("C003", file, node.lineno, f"`{raw}(…)` calls the network: use `ctx.http`")
        )
    if (
        full in {"os.getenv", "os.environ.get", "os.putenv"}
        or full == "dotenv.load_dotenv"
        or last == "load_dotenv"
    ):
        out.append(finding("C004", file, node.lineno, f"`{raw}(…)` reads the process environment"))
    if full in _FS_WRITE_CALLS:
        out.append(
            finding("C005", file, node.lineno, f"`{raw}(…)` changes files: use `ctx.storage`")
        )
    if (
        last in _PATH_WRITE
        and isinstance(node.func, ast.Attribute)
        and _looks_like_path(node.func.value, names)
    ):
        out.append(
            finding("C005", file, node.lineno, f"`.{last}()` changes files: use `ctx.storage`")
        )
    if full in {"open", "io.open", "builtins.open"} or raw == "open":
        mode = "r"
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        out.append(
            finding(
                "C005", file, node.lineno, f"`open(…, {mode!r})` writes a file: use `ctx.storage`"
            )
            if any(c in mode for c in "wax+")
            else finding(
                "C005",
                file,
                node.lineno,
                "`open(…)` reads a file: bundle the data as an artifact",
                "warning",
            )
        )
    if (
        last in _PATH_READ
        and isinstance(node.func, ast.Attribute)
        and _looks_like_path(node.func.value, names)
    ):
        out.append(
            finding(
                "C005",
                file,
                node.lineno,
                f"`.{last}()` reads a file: bundle the data as an artifact",
                "warning",
            )
        )
    if full in _NONDETERMINISTIC:
        out.append(
            finding("C006", file, node.lineno, f"`{raw}(…)` is not a function of the inputs")
        )
    elif last in _NOW and ("datetime" in full or "date" in full):
        out.append(finding("C006", file, node.lineno, f"`{raw}(…)` reads the clock"))
    return out


def _looks_like_path(node: ast.AST, names: dict[str, str]) -> bool:
    d = _resolve(_dotted(node), names)
    return (
        d.startswith(("pathlib.", "Path"))
        or d.split(".")[-1] in {"Path", "PurePath"}
        or isinstance(node, ast.BinOp)
    )


# ── the project ──────────────────────────────────────────────────────────────


def python_files(project: Path) -> list[Path]:
    root = project / "agents"
    return (
        sorted(
            p
            for p in root.rglob("*.py")
            if "_generated" not in p.parts and "__pycache__" not in p.parts
        )
        if root.is_dir()
        else []
    )


def run_ruff(project: Path, files: list[Path]) -> tuple[list[Finding], str]:
    """Findings of ``ruff`` for *files* and a note when it could not run."""
    if not files:
        return [], ""
    exe = shutil.which("ruff")
    argv = [exe] if exe else [sys.executable, "-m", "ruff"]
    try:
        done = subprocess.run(
            [*argv, "check", "--no-cache", "--output-format", "json", *[str(f) for f in files]],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"ruff did not run: {exc}"
    try:
        items = json.loads(done.stdout or "[]")
    except json.JSONDecodeError:
        return [], "ruff is not installed (pip install 'tuvl[standard]'): its checks were skipped"
    out: list[Finding] = []
    for item in items:
        path = Path(item.get("filename", ""))
        shown = (
            str(path.relative_to(project))
            if path.is_absolute() and path.is_relative_to(project)
            else str(path)
        )
        code = str(item.get("code") or "")
        severity: Literal["error", "warning"] = (
            "error"
            if code.startswith(("F82", "F63", "F7", "E9")) or code in {"F821", "F811"}
            else "warning"
        )
        out.append(
            finding(
                "C900",
                shown,
                int((item.get("location") or {}).get("row", 1)),
                f"ruff {code}: {item.get('message', '')}",
                severity,
            )
        )
    return out, ""


def review_project(
    project: Path, files: list[Path] | None = None, *, with_ruff: bool = True
) -> Report:
    """Review *files* (default: every agent's Python) against the rules above."""
    chosen = files if files is not None else python_files(project)
    chosen = [f if f.is_absolute() else project / f for f in chosen]
    agents = agents_in_workflows(project)
    report = Report(files=[_rel(project, f) for f in chosen])
    for path in chosen:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = _rel(project, path)
        in_agents = "agents" in Path(rel).parts
        report.findings += review_source(
            path.read_text(), rel, agents=agents if in_agents else None, in_agents_dir=in_agents
        )
    if with_ruff:
        ruff_findings, note = run_ruff(
            project, [f for f in chosen if f.suffix == ".py" and f.is_file()]
        )
        report.findings += ruff_findings
        if note:
            report.notes.append(note)
    # one finding per place and rule
    seen: set[tuple[str, str, int, str]] = set()
    unique: list[Finding] = []
    for f in report.findings:
        key = (f.code, f.file, f.line, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    report.findings = unique
    return report


def _rel(project: Path, path: Path) -> str:
    return str(path.relative_to(project)) if path.is_relative_to(project) else str(path)
