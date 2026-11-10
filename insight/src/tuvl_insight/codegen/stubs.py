"""Code-agent stubs (spec §17.6): ``agents/<run>.py`` is created when missing,
otherwise merged with libcst — the ``@agent`` decorator, the parameter and
return annotations, ``async`` and the docstring are regenerated from the
contract; the function body, the parameter names and everything else in the
file are preserved.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence

from tuvl.extras import require

require("codegen")

import libcst as cst  # noqa: E402

from tuvl_insight.codegen.schemas import AgentNames  # noqa: E402

SCHEMAS_MODULE = "agents._generated.schemas"
Stmt = cst.SimpleStatementLine | cst.BaseCompoundStatement
_WIDTH = 88


def function_name(run: str) -> str:
    return run.replace(".", "_")


def docstring(text: str, indent: str = "    ") -> str:
    words = " ".join(text.split()).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if len(indent) + len(words) + 6 <= _WIDTH:
        return f'"""{words}"""'
    lines = textwrap.wrap(words, _WIDTH - len(indent))
    return '"""\n' + "".join(f"{indent}{line}\n" for line in lines) + f'{indent}"""'


def _annotations(names: AgentNames | None) -> tuple[str, str]:
    return (names.inputs, names.returns) if names else ("Any", "Any")


def new_stub(run: str, names: AgentNames | None, description: str) -> str:
    inputs, returns = _annotations(names)
    lines = ["from __future__ import annotations", ""]
    if names is None:
        lines += ["from typing import Any", ""]
    lines += ["from tuvl import Ctx, agent", ""]
    if names and names.imports:
        lines += [f"from {SCHEMAS_MODULE} import {', '.join(sorted(names.imports))}", ""]
    lines += [
        "",
        f'@agent("{run}")',
        f"async def {function_name(run)}(inp: {inputs}, ctx: Ctx) -> {returns}:",
        f"    {docstring(description)}",
        f'    raise NotImplementedError("{run}")',
    ]
    return "\n".join(lines) + "\n"


def _is_agent_decorator(decorator: cst.Decorator, run: str) -> bool:
    call = decorator.decorator
    if not isinstance(call, cst.Call) or not call.args:
        return False
    func = call.func
    named = (isinstance(func, cst.Name) and func.value == "agent") or (
        isinstance(func, cst.Attribute) and func.attr.value == "agent"
    )
    first = call.args[0].value
    return named and isinstance(first, cst.SimpleString) and first.evaluated_value == run


def _is_docstring(statement: cst.BaseStatement | cst.BaseSmallStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.Expr)
        and isinstance(statement.body[0].value, cst.SimpleString | cst.ConcatenatedString)
    )


class _Agent(cst.CSTTransformer):
    def __init__(self, run: str, names: AgentNames | None, description: str) -> None:
        self.run = run
        self.inputs, self.returns = _annotations(names)
        self.description = description
        self.found = False

    def leave_FunctionDef(  # noqa: N802 — libcst visitor name
        self, original: cst.FunctionDef, updated: cst.FunctionDef
    ) -> cst.FunctionDef:
        if not any(_is_agent_decorator(d, self.run) for d in original.decorators):
            return updated
        self.found = True
        decorators = [
            d.with_changes(decorator=cst.parse_expression(f'agent("{self.run}")'))
            if _is_agent_decorator(d, self.run)
            else d
            for d in updated.decorators
        ]
        params = list(updated.params.params)
        typed = [cst.Annotation(cst.parse_expression(self.inputs)), cst.Annotation(cst.Name("Ctx"))]
        if len(params) >= 2:
            params[0] = params[0].with_changes(annotation=typed[0])
            params[1] = params[1].with_changes(annotation=typed[1])
        else:
            params = [
                cst.Param(cst.Name("inp"), annotation=typed[0]),
                cst.Param(cst.Name("ctx"), annotation=typed[1]),
            ]
        doc = cst.SimpleStatementLine([cst.Expr(cst.SimpleString(docstring(self.description)))])
        suite = updated.body
        if isinstance(suite, cst.IndentedBlock):  # a one-line `def f(): ...` keeps its body as is
            body: list[cst.BaseStatement] = list(suite.body)
            body = [doc, *body[1:]] if body and _is_docstring(body[0]) else [doc, *body]
            suite = suite.with_changes(body=body)
        return updated.with_changes(
            decorators=decorators,
            params=updated.params.with_changes(params=params),
            returns=cst.Annotation(cst.parse_expression(self.returns)),
            asynchronous=cst.Asynchronous(),
            body=suite,
        )


def _module_name(node: cst.BaseExpression | None) -> str:
    if node is None:
        return ""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_module_name(node.value)}.{node.attr.value}"
    return ""


def _import_from(statement: Stmt) -> cst.ImportFrom | None:
    if isinstance(statement, cst.SimpleStatementLine) and len(statement.body) == 1:
        node = statement.body[0]
        if isinstance(node, cst.ImportFrom):
            return node
    return None


def _is_import(statement: Stmt) -> bool:
    return isinstance(statement, cst.SimpleStatementLine) and all(
        isinstance(n, cst.Import | cst.ImportFrom) for n in statement.body
    )


def _ensure_names(body: list[Stmt], module: str, wanted: Sequence[str]) -> list[Stmt]:
    """Make ``from <module> import <wanted>`` hold, extending an existing import."""
    for i, statement in enumerate(body):
        node = _import_from(statement)
        if (
            node is None
            or _module_name(node.module) != module
            or isinstance(node.names, cst.ImportStar)
        ):
            continue
        have = {a.name.value for a in node.names if isinstance(a.name, cst.Name)}
        missing = [n for n in wanted if n not in have]
        if missing:
            names = [*node.names, *(cst.ImportAlias(cst.Name(n)) for n in missing)]
            names = [a.with_changes(comma=cst.MaybeSentinel.DEFAULT) for a in names]
            body[i] = statement.with_changes(body=[node.with_changes(names=names)])
        return body
    return _insert_after_imports(
        body, cst.parse_statement(f"from {module} import {', '.join(wanted)}\n")
    )


def _insert_after_imports(body: list[Stmt], statement: Stmt) -> list[Stmt]:
    last = max((i for i, s in enumerate(body) if _is_import(s)), default=-1)
    if last == -1 and body and _is_docstring(body[0]):
        last = 0
    if isinstance(statement, cst.SimpleStatementLine) and last >= 0:
        statement = statement.with_changes(leading_lines=[cst.EmptyLine()])
    return [*body[: last + 1], statement, *body[last + 1 :]]


class _Uses(cst.CSTVisitor):
    def __init__(self, name: str) -> None:
        self.name = name
        self.count = 0

    def visit_ImportAlias(self, node: cst.ImportAlias) -> bool:  # noqa: N802 — libcst visitor name
        return False

    def visit_Name(self, node: cst.Name) -> None:  # noqa: N802 — libcst visitor name
        self.count += node.value == self.name


def _drop_unused(body: list[Stmt], module: str, name: str) -> list[Stmt]:
    """Remove ``name`` from ``from <module> import …`` when nothing uses it."""
    uses = _Uses(name)
    cst.Module(body=body).visit(uses)
    if uses.count:
        return body
    out: list[Stmt] = []
    for statement in body:
        node = _import_from(statement)
        if (
            node is None
            or _module_name(node.module) != module
            or isinstance(node.names, cst.ImportStar)
        ):
            out.append(statement)
            continue
        kept = [
            a for a in node.names if not (isinstance(a.name, cst.Name) and a.name.value == name)
        ]
        if kept:
            kept[-1] = kept[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
            out.append(statement.with_changes(body=[node.with_changes(names=kept)]))
    return out


def merge_stub(source: str, run: str, names: AgentNames | None, description: str) -> str:
    """Regenerate the contract-owned parts of an existing agent file."""
    module = cst.parse_module(source)
    transformer = _Agent(run, names, description)
    module = module.visit(transformer)
    body = [
        s
        for s in module.body
        if not ((node := _import_from(s)) and _module_name(node.module) == SCHEMAS_MODULE)
    ]
    if not transformer.found:
        stub = cst.parse_module(new_stub(run, names, description)).body[-1]
        body.append(stub.with_changes(leading_lines=[cst.EmptyLine(), cst.EmptyLine()]))
    body = _ensure_names(body, "tuvl", ["Ctx", "agent"])
    if names is None:
        body = _ensure_names(body, "typing", ["Any"])
    else:
        body = _drop_unused(body, "typing", "Any")  # the old `inp: Any` annotation was its user
    if names is not None and names.imports:
        line = f"from {SCHEMAS_MODULE} import {', '.join(sorted(names.imports))}\n"
        body = _insert_after_imports(body, cst.parse_statement(line))
    return module.with_changes(body=body).code
