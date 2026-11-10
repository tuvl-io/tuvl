"""``tuvl lint spec`` (proposals/spec-referee.md §6): is the spec complete and consistent?

Zero tokens, deterministic. Rules S001–S021 over the grammar parsed by :mod:`tuvl.spec.grammar`;
rule conditions are checked with tuvl's own expression checker, and the engine-implied signals
mirror :func:`tuvl_insight.validate.signals.signals_of`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, create_model

from tuvl.contract.signals import HTTP_STATUS, is_reserved
from tuvl.contract.tel import TELError, compile_expr
from tuvl.contract.types import TypeParseError, parse_type, registry_resolver
from tuvl_insight.spec.grammar import (
    COLUMN_TYPES,
    DataModel,
    Flow,
    Step,
    Structure,
    parse_structure,
)
from tuvl_insight.spec.parse import Spec

Severity = Literal["error", "warning", "waived"]


@dataclass
class Finding:
    code: str
    severity: Severity
    line: int
    message: str
    file: str | None = None  # set when the finding is in another file (the task list)

    def to_json(self) -> dict[str, Any]:
        out = {
            "code": self.code,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
        }
        return {**out, "file": self.file} if self.file else out


@dataclass
class CheckResult:
    spec: str
    path: str
    findings: list[Finding] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)  # prose sections
    structure: Structure | None = None
    hash: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)  # anchor, line, hash (for tasks)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def add(self, code: str, line: int, message: str, severity: Severity = "error") -> None:
        self.findings.append(Finding(code, severity, line, message))

    def to_json(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "path": self.path,
            "ok": not self.errors,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [f.to_json() for f in sorted(self.findings, key=_order)],
            "unchecked": self.unchecked,
            "hash": self.hash,
            "sections": self.sections,
        }


def _order(f: Finding) -> tuple[int, int]:
    return ({"error": 0, "warning": 1, "waived": 2}[f.severity], f.line)


def check_spec(
    spec: Spec,
    text: str,
    *,
    existing_models: Mapping[str, set[str]] | None = None,
    workflows: set[str] | None = None,
) -> CheckResult:
    """Check *spec* (parsed by :func:`tuvl.spec.parse.parse_text` from *text*).

    *existing_models* (name → field names) and *workflows* describe the project, for
    ``(existing)`` models and loop tools that call another workflow.
    """
    structure = parse_structure(text)
    out = CheckResult(
        spec.name,
        spec.path,
        unchecked=structure.prose,
        structure=structure,
        hash=spec.hash,
        sections=[{"anchor": s.anchor, "line": s.line, "hash": s.hash} for s in spec.sections],
    )
    for problem in structure.problems:
        out.add("S001", problem.line, problem.message)
    if not structure.has_flows and not structure.has_data:
        out.add("S001", 1, "the spec has no `# Data` or `# Flows` section — nothing can be checked")
        return out
    existing = existing_models or {}
    known_project = existing_models is not None
    resolver = _resolver(structure.data)
    _check_data(out, structure, existing, known_project=known_project)
    secure_fields = {
        f.name for m in structure.data.values() for f in m.fields.values() if "secure" in f.flags
    }
    secure_models = {
        m.name
        for m in structure.data.values()
        if any("secure" in f.flags for f in m.fields.values())
    }
    for flow in structure.flows.values():
        _check_flow(
            out, structure, flow, resolver, secure_fields, secure_models, workflows or set()
        )
    _check_examples(out, spec, structure, existing)
    for line in structure.acceptance:
        out.add(
            "S014",
            line,
            "free-text acceptance bullet — not checked; write it as a tuvl-example",
            "warning",
        )
    return out


# ── Data ─────────────────────────────────────────────────────────────────────


def _check_data(
    out: CheckResult, s: Structure, existing: Mapping[str, set[str]], *, known_project: bool
) -> None:
    for model in s.data.values():
        if known_project and not model.new and model.name not in existing:
            out.add("S007", model.line, f"{model.name} is marked (existing) but is not in models/")
        known = existing.get(model.name)
        for f in model.fields.values():
            if f.type not in COLUMN_TYPES:
                out.add(
                    "S007", model.line, f"{model.name}.{f.name}: unknown column type '{f.type}'"
                )
            if f.ref and f.ref not in s.data and (known_project and f.ref not in existing):
                out.add(
                    "S007", model.line, f"{model.name}.{f.name} refers to unknown model '{f.ref}'"
                )
            if f.type == "enum" and f.default and f.default not in f.enum:
                out.add(
                    "S007",
                    model.line,
                    f"{model.name}.{f.name}: default '{f.default}' is not one of its values",
                )
            if not model.new and known is not None and f.name not in known:
                out.add(
                    "S007",
                    model.line,
                    f"{model.name}.{f.name} is not a field of the existing model",
                )


# ── signals ──────────────────────────────────────────────────────────────────


def _enum(t: str) -> list[str]:
    try:
        ref = parse_type(t)
    except TypeParseError:
        return []
    return list(ref.values) if ref.kind == "enum" and ref.values else []


def _tool_signals(step: Step) -> tuple[set[str], set[str]]:
    """(required, optional) reserved signals of a tool step, as ``tuvl lint project`` derives them.

    A spec cannot say ``if_exists``, so ``put`` and ``copy`` take the default (``fail``): they
    emit ``conflict``, which is routed or waived.
    """
    d = step.detail
    kind, op = (d[1] if len(d) > 1 else ""), (d[2] if len(d) > 2 else "")
    if kind == "db":
        if op in ("read", "delete"):
            return {"not_found"}, set()
        if op in ("create", "update", "upsert"):
            return {"conflict"}, set()
    elif kind == "http":
        return set(), {"http_*"}
    elif kind == "storage":
        required = {"not_found"} if op in ("get", "head", "copy") else set()
        if op in ("put", "copy"):
            required.add("conflict")
        return required, {"too_large"} if op in ("put", "get", "copy") else set()
    return set(), set()


def can_emit(step: Step, signal: str) -> bool:
    normal, required = signals(step)
    optional = _tool_signals(step)[1] if step.engine == "tool" else set()
    return (
        signal == "error"
        or signal in normal | required | optional
        or (HTTP_STATUS.fullmatch(signal) is not None and "http_*" in optional)
    )


def signals(step: Step) -> tuple[set[str], set[str]]:
    """(normal, required) signals of *step*."""
    declared = {
        sig for r in step.routes for sig in r.signals if not is_reserved(sig) and sig != "default"
    }
    if step.engine == "tool":
        return {"default"}, _tool_signals(step)[0]
    guarded = {"guardrail_violation"} if step.guardrails else set()
    if step.engine == "llm":
        return (declared or {"default"}), {"parse_error", "timeout", "budget_exceeded"} | guarded
    if step.engine == "loop":
        return declared, {"max_iterations", "budget_exceeded", "timeout", "aborted"} | guarded
    if step.engine == "decide":
        enums = [v for t in step.outputs.values() if (v := _enum(t))]
        return (set(enums[0]) if len(enums) == 1 else set()), (
            {"low_confidence"} if step.model_backed else set()
        )
    if step.engine == "human":
        normal = {"default"}
        for t in step.outputs.values():
            if t.rstrip("?") == "bool":
                normal = {"true", "false"}
                break
            if values := _enum(t):
                normal = set(values)
                break
        return normal, ({"expired"} if step.expires else set())
    return declared | {"default"}, set()  # code: the routes, plus default to the next step


def loop_tools(flow: Flow) -> set[str]:
    """Steps that only a loop calls: off the routing graph."""
    return {t.name for x in flow.steps for t in x.tools if t.kind == "step"}


def compensators(flow: Flow) -> set[str]:
    """Steps that only run to undo another on a rollback end: off the routing graph."""
    return {x.compensate for x in flow.steps if x.compensate}


def off_graph(flow: Flow) -> set[str]:
    return loop_tools(flow) | compensators(flow)


def routes_of(flow: Flow, step: Step) -> dict[str, str]:
    """signal → target, with the implicit ``default`` (next spine step or END) and ``error``."""
    out = {sig: r.target for r in step.routes for sig in r.signals}
    out.setdefault("error", "END.failed")
    normal, _ = signals(step)
    if "default" in normal and "default" not in out and not step.off_spine:
        spine = [x for x in flow.steps if not x.off_spine]
        i = spine.index(step)
        out["default"] = spine[i + 1].id if i + 1 < len(spine) else "END"
    return out


# ── Flows ────────────────────────────────────────────────────────────────────


def _check_flow(
    out: CheckResult,
    s: Structure,
    flow: Flow,
    resolver: Callable[[str, str], type[BaseModel] | None],
    secure_fields: set[str],
    secure_models: set[str],
    workflows: set[str],
) -> None:
    by_id = {x.id: x for x in flow.steps}
    if not flow.has_trigger:
        out.add("S001", flow.line, f"flow {flow.name}: no `Trigger:` line")
    elif flow.access is None and flow.schedule is None and not flow.tool_only:
        out.add(
            "S010",
            flow.line,
            f"flow {flow.name}: the trigger declares no access (group <g> | public | signed-in)",
        )
    for need in ("default", "failed"):
        if need not in flow.ends:
            out.add("S001", flow.line, f"flow {flow.name}: `Ends:` must list `{need}`")
    if not any(not x.off_spine for x in flow.steps):
        out.add("S001", flow.line, f"flow {flow.name}: no numbered steps")
        return
    for name, t in flow.inputs.items():
        _check_type(out, s, flow.line, f"input `{name}`", t)
    for step in flow.steps:
        for name, t in step.outputs.items():
            _check_type(out, s, step.line, f"`{step.id}`.{name}", t)
        db = step.engine == "tool" and len(step.detail) > 3 and step.detail[1] == "db"
        if db and step.detail[3] not in s.data:
            out.add("S007", step.line, f"`{step.id}`: model '{step.detail[3]}' is not in # Data")
        if step.id not in off_graph(flow):
            _check_routes(out, flow, step, by_id)
        _check_model_safety(out, flow, step, secure_fields, secure_models)
        if step.engine == "decide":
            _check_rules(out, flow, step, resolver)
        if step.engine == "human":
            if not step.group:
                out.add("S013", step.line, f"`{step.id}`: a human step without a group", "warning")
            if not step.expires:
                out.add(
                    "S013", step.line, f"`{step.id}`: a human step without an expiry", "warning"
                )
        for sig, (why, line) in step.waivers.items():
            out.add("WAIVED", line, f"`{step.id}`: '{sig}' waived — {why}", "waived")
    _check_loop_tools(out, s, flow, workflows)
    _check_reachability(out, flow, by_id)


def _check_type(out: CheckResult, s: Structure, line: int, where: str, t: str) -> None:
    try:
        ref = parse_type(t)
    except TypeParseError as exc:
        out.add("S001", line, f"{where}: bad type '{t}' ({exc})")
        return
    base = ref.item if ref.kind == "list" and ref.item is not None else ref
    if base.kind == "model" and base.name not in s.data:
        out.add("S007", line, f"{where}: model '{base.name}' is not in # Data")


def _check_routes(out: CheckResult, flow: Flow, step: Step, by_id: Mapping[str, Step]) -> None:
    normal, required = signals(step)
    routes = routes_of(flow, step)
    if step.engine == "loop" and not normal:
        out.add("S004", step.line, f"`{step.id}` (loop) needs at least one outcome route")
    for sig, target in routes.items():
        if not can_emit(step, sig):
            out.add(
                "S004", step.line, f"`{step.id}` ({' '.join(step.detail[:3])}) never emits '{sig}'"
            )
        if target.startswith("END."):
            if target[4:] not in flow.ends:
                out.add(
                    "S002",
                    step.line,
                    f"`{step.id}`: {sig} → {target}, but `{target[4:]}` is not in `Ends:`",
                )
        elif target != "END":
            if target not in by_id:
                out.add("S002", step.line, f"`{step.id}`: {sig} → unknown step `{target}`")
            elif target in off_graph(flow):
                out.add(
                    "S002",
                    step.line,
                    f"`{step.id}`: {sig} → `{target}`, a loop tool or compensator",
                )
    for sig in sorted((normal | required) - set(routes) - set(step.waivers)):
        out.add("S004", step.line, f"`{step.id}` can emit '{sig}' — route it or waive it")


def _check_model_safety(
    out: CheckResult, flow: Flow, step: Step, secure_fields: set[str], secure_models: set[str]
) -> None:
    if step.engine == "decide":
        if step.model_backed:
            if not step.model_ref:
                out.add("S005", step.line, f"`{step.id}`: decide model without a model reference")
            if step.threshold is None:
                out.add(
                    "S005",
                    step.line,
                    f"`{step.id}`: decide model without a confidence threshold (≥ 0.x)",
                )
            if "s" not in step.budget:
                out.add("S005", step.line, f"`{step.id}`: decide model without a time budget")
        elif not step.rules:
            out.add("S005", step.line, f"`{step.id}`: decide with neither rules nor a model")
        elif step.rules[-1][0] != "otherwise":
            out.add(
                "S005",
                step.line,
                f"`{step.id}`: rules have no `otherwise` and no model to fall back on",
            )
    if step.engine in ("llm", "loop"):
        if not step.model_ref:
            out.add("S005", step.line, f"`{step.id}` ({step.engine}) names no model")
        if "tok" not in step.budget or "s" not in step.budget:
            out.add(
                "S005",
                step.line,
                f"`{step.id}` ({step.engine}) needs a budget with tokens and time",
            )
    if step.model_backed:
        for name in step.inputs:
            t = (_type_of(flow, name) or "").rstrip("?")
            base = t[5:-1] if t.startswith("list[") else t
            if name in secure_fields or base in secure_models:
                out.add(
                    "S008",
                    step.line,
                    f"`{step.id}` sends '{name}' to a model, but it is (or contains) a secure field",
                )


def _check_rules(
    out: CheckResult, flow: Flow, step: Step, resolver: Callable[[str, str], type[BaseModel] | None]
) -> None:
    values = set(_enum(next(iter(step.outputs.values()), ""))) if step.outputs else set()
    env = {k: t for k in step.inputs if (t := _type_of(flow, k))}
    for expr, value, line in step.rules:
        if values and value not in values:
            out.add(
                "S015", line, f"`{step.id}`: rule result '{value}' is not one of {sorted(values)}"
            )
        if expr == "otherwise":
            continue
        try:
            compiled = compile_expr(expr)
            unknown = compiled.roots() - set(step.inputs)
            if unknown:
                out.add(
                    "S015",
                    line,
                    f"`{step.id}`: rule reads {sorted(unknown)}, not among the step's inputs",
                )
            else:
                compiled.check(env, resolver, expect="bool")
        except TELError as exc:
            out.add("S015", line, f"`{step.id}`: rule `{expr}` — {exc}")


def _check_loop_tools(out: CheckResult, s: Structure, flow: Flow, workflows: set[str]) -> None:
    used: set[str] = set()
    for step in flow.steps:
        for tool in step.tools:
            if step.engine != "loop":
                out.add("S016", step.line, f"`{step.id}`: only a loop step has tools")
                break
            if tool.kind == "step":
                target = flow.step(tool.name)
                if target is None or not target.off_spine:
                    out.add(
                        "S016",
                        step.line,
                        f"`{step.id}`: tool `{tool.name}` is not a T step of this flow",
                    )
                used.add(tool.name)
            elif (
                tool.kind == "workflow" and tool.name not in s.flows and tool.name not in workflows
            ):
                out.add(
                    "S016",
                    step.line,
                    f"`{step.id}`: tool workflow `{tool.name}` is not a `## Flow:` or a project workflow",
                )
            if tool.approval_when:
                try:
                    compile_expr(tool.approval_when)
                except TELError as exc:
                    out.add(
                        "S016",
                        step.line,
                        f"`{step.id}`: approval condition for `{tool.name}` — {exc}",
                    )
        if step.engine == "loop" and not step.tools:
            out.add("S016", step.line, f"`{step.id}` (loop) lists no `tools:`")
    for step in flow.steps:
        routed = any(r.target == step.id for x in flow.steps for r in x.routes)
        if (
            step.compensate
            and (target := flow.step(step.compensate)) is not None
            and not target.off_spine
        ):
            out.add(
                "S016", step.line, f"`{step.id}`: compensator `{step.compensate}` must be a T step"
            )
        elif step.compensate and target is None:
            out.add(
                "S016",
                step.line,
                f"`{step.id}`: compensator `{step.compensate}` is not a step of this flow",
            )
        if (
            step.off_spine
            and step.id not in used
            and not routed
            and step.id not in compensators(flow)
        ):
            out.add(
                "S016",
                step.line,
                f"`{step.id}` is an off-spine step nothing uses (no loop tool, no route)",
            )
        if (
            step.tool_of
            and (loop := flow.step(step.tool_of)) is not None
            and step.id not in {t.name for t in loop.tools}
        ):
            out.add(
                "S016",
                step.line,
                f"`{step.id}` says `tool of {step.tool_of}`, but that loop doesn't list it",
            )


def _check_reachability(out: CheckResult, flow: Flow, by_id: Mapping[str, Step]) -> None:
    entry = next(x for x in flow.steps if not x.off_spine)
    avail: dict[str, set[str]] = {}  # names produced on every path to the step
    maybe: dict[str, set[str]] = {}  # names produced on some path to the step
    reached_ends: set[str] = set()
    work = [(entry.id, set(flow.inputs))]
    while work:
        sid, have = work.pop()
        if sid in avail:
            if have >= avail[sid] and have <= maybe[sid]:
                continue
            maybe[sid] |= have
            avail[sid] &= have
            have = avail[sid] | maybe[sid]
        else:
            avail[sid], maybe[sid] = set(have), set(have)
        step = by_id[sid]
        normal, _ = signals(step)
        for sig, target in routes_of(flow, step).items():
            produced = set(step.outputs) if sig in normal else set()
            produced |= {o for r in step.routes if sig in r.signals for o in r.outputs}
            if target == "END":
                reached_ends.add("default")
            elif target.startswith("END."):
                reached_ends.add(target[4:])
            elif target in by_id and target not in off_graph(flow):
                work.append((target, have | produced))
    for name in flow.ends:
        if name not in reached_ends and name != "failed":
            out.add("S003", flow.line, f"flow {flow.name}: end `{name}` is unreachable")
    for step in flow.steps:
        if step.id in off_graph(flow):
            continue
        if step.id not in avail:
            out.add("S003", step.line, f"`{step.id}` is unreachable from the trigger", "warning")
            continue
        missing = [
            i for i in step.inputs if i not in avail[step.id] and i not in step.optional_inputs
        ]
        if missing:
            out.add(
                "S006", step.line, f"`{step.id}` reads {missing}, not produced on every path to it"
            )
        clash = [o for o in step.outputs if o in maybe[step.id]]
        if clash:
            out.add(
                "S009",
                step.line,
                f"`{step.id}` outputs {clash}, already named by the trigger input or an earlier step",
            )


def _type_of(flow: Flow, name: str) -> str | None:
    if name in flow.inputs:
        return flow.inputs[name]
    for x in flow.steps:
        if name in x.outputs:
            return x.outputs[name]
    return None


_PY: dict[str, Any] = {
    "string": str,
    "text": str,
    "integer": int,
    "numeric": float,
    "boolean": bool,
    "uuid": uuid.UUID,
    "date": dt.date,
    "timestamptz": dt.datetime,
    "jsonb": dict,
}


def _resolver(data: Mapping[str, DataModel]) -> Callable[[str, str], type[BaseModel] | None]:
    built: dict[str, type[BaseModel]] = {}

    def resolve(name: str, variant: str) -> type[BaseModel] | None:
        model = data.get(name)
        if model is None or not model.new:
            # an (existing) model is the project's — its listed fields are only what the spec uses
            found = registry_resolver(name, variant)
            if found is not None or model is None:
                return found
        if name not in built:
            fields: dict[str, Any] = {}
            for f in model.fields.values():
                py = Literal[tuple(f.enum)] if f.type == "enum" and f.enum else _PY.get(f.type, Any)
                fields[f.name] = (py | None, None)
            built[name] = create_model(name, **fields)
        return built[name]

    return resolve


# ── Acceptance examples ──────────────────────────────────────────────────────


def _example_flow(spec: Spec, s: Structure, data: Mapping[str, Any]) -> str | None:
    wf = data.get("workflow") or spec.front.get("workflow")
    if wf:
        return str(wf)
    return next(iter(s.flows)) if len(s.flows) == 1 else None


def _check_examples(
    out: CheckResult, spec: Spec, s: Structure, existing: Mapping[str, set[str]]
) -> None:
    covered: set[tuple[str | None, str | None]] = set()
    for ex in spec.examples:
        data, line, name = ex.data, ex.line, ex.name
        wf = _example_flow(spec, s, data)
        expect = data.get("expect") or {}
        covered.add((wf, expect.get("end")))
        if wf not in s.flows:
            out.add("S012", line, f"example '{name}': workflow '{wf}' is not a `## Flow:`")
            continue
        flow = s.flows[wf]
        for model in data.get("given") or {}:
            if model not in s.data and model not in existing:
                out.add("S012", line, f"example '{name}': given model '{model}' is not in # Data")
        for sid, mock in (data.get("mocks") or {}).items():
            step = flow.step(sid)
            if step is None:
                out.add("S012", line, f"example '{name}': mocks unknown step `{sid}`")
                continue
            fault_only = (mock or {}).get("signal") == "error" and not (mock or {}).get("outputs")
            if (
                step.engine in ("code", "tool", "decide")
                and not (step.model_backed or step.detail[1:2] in (["http"], ["mcp"]))
                and not fault_only
            ):
                out.add(
                    "T001",
                    line,
                    f"example '{name}': mocks deterministic step `{sid}` ({step.engine}) — seed `given:` / `as:` instead",
                    "warning",
                )
            mock = mock or {}
            sig = str(mock.get("signal", "default"))
            normal, _ = signals(step)
            if not can_emit(step, sig):
                out.add("S012", line, f"example '{name}': `{sid}` cannot emit '{sig}'")
            declared = (
                set(step.outputs)
                if sig in normal
                else {o for r in step.routes if sig in r.signals for o in r.outputs}
            )
            extra = set(mock.get("outputs") or {}) - declared
            if extra:
                out.add(
                    "S012",
                    line,
                    f"example '{name}': the mock of `{sid}` sets {sorted(extra)}, not declared for '{sig}'",
                )
        end = expect.get("end")
        if end and end not in flow.ends:
            out.add("S012", line, f"example '{name}': expected end '{end}' is not in `Ends:`")
    for flow in s.flows.values():
        for end in flow.ends:
            if end == "failed" or (flow.name, end) in covered or end in flow.no_example:
                continue
            out.add(
                "S011",
                flow.line,
                f"flow {flow.name}: end `{end}` has no acceptance example",
                "warning",
            )
        for end, (why, line) in flow.no_example.items():
            if end not in flow.ends:
                out.add(
                    "S001",
                    line,
                    f"flow {flow.name}: `{end}: no example` names an end not in `Ends:`",
                )
            else:
                out.add(
                    "WAIVED",
                    line,
                    f"flow {flow.name}: end `{end}` has no example — {why}",
                    "waived",
                )


# ── the project ──────────────────────────────────────────────────────────────


def project_context() -> tuple[dict[str, set[str]], set[str]]:
    """The loaded project's models (name → fields) and workflow names."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.models.schemas import SCHEMA_REGISTRY

    models = {
        name: set(variants["read"].model_fields)
        for name, variants in SCHEMA_REGISTRY.items()
        if "read" in variants
    }
    return models, set(WORKFLOWS)


def check_project(project: Path, specs: list[Path] | None = None) -> list[CheckResult]:
    """Check *specs* (default: ``specs/*.md``) against the loaded project.

    Raises :class:`tuvl.spec.parse.SpecError` when a file isn't a spec at all.
    """
    from tuvl_insight.spec.parse import parse_text

    models, workflows = project_context()
    files = (
        [p if p.is_absolute() else project / p for p in specs]
        if specs
        else sorted((project / "specs").glob("*.md"))
    )
    results = []
    for file in files:
        text = file.read_text()
        shown = str(file.relative_to(project)) if file.is_relative_to(project) else str(file)
        spec = parse_text(text, path=shown, default_name=file.stem)
        result = check_spec(spec, text, existing_models=models, workflows=workflows)
        structure = result.structure
        if structure is not None and (structure.has_flows or structure.has_data):
            from tuvl_insight.spec.tasks import plan_findings

            result.findings += plan_findings(project, spec, structure)
        from tuvl_insight.spec.lifecycle import lifecycle_findings

        result.findings += [
            Finding(code, severity, 1, message)  # type: ignore[arg-type]
            for code, severity, message in lifecycle_findings(project, file)
        ]
        results.append(result)
    return results


def check_report(project: Path, spec: str | None = None, *, strict: bool = False) -> dict[str, Any]:
    """The ``tuvl lint spec --json`` document."""
    results = check_project(project, [Path(spec)] if spec else None)
    ok = all(not r.errors and not (strict and r.warnings) for r in results)
    return {"ok": ok, "specs": [r.to_json() for r in results]}
