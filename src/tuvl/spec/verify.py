"""``tuvl verify spec`` (proposals/spec-referee.md §8): does the implementation conform to the spec?

Zero tokens. Compares each ``## Flow:`` with its workflow step by step (R001–R008), each Data model
with its ModelDefinition (R006), and each example and step with honest test results (R009–R010).
Contradictions are errors; work not done yet (pending agents, missing workflows, untested steps)
are warnings, which ``--strict`` turns into failures.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl.contract.schema import CollectionToolRef, duration_seconds
from tuvl.contract.types import TypeParseError, TypeRef, parse_type
from tuvl.spec.check import Finding, Severity, off_graph, routes_of
from tuvl.spec.grammar import DataModel, Flow, Step, Structure, parse_structure
from tuvl.spec.parse import Spec, slug

#: Spec column type → the ModelDefinition types that satisfy it.
COLUMN_EQUIVALENTS: dict[str, set[str]] = {
    "string": {"string", "varchar"},
    "text": {"text"},
    "integer": {"integer", "bigint", "smallint"},
    "numeric": {"numeric", "float", "decimal"},
    "boolean": {"boolean"},
    "uuid": {"uuid"},
    "date": {"date"},
    "timestamptz": {"timestamptz", "timestamp"},
    "jsonb": {"jsonb", "json"},
    "enum": {"enum"},
}


@dataclass
class TestUse:
    """What the project's tests say about one step."""

    __test__ = False

    passed: list[str] = field(default_factory=list)  # passing tests that ran it for real
    mocked: list[str] = field(default_factory=list)  # passing tests that only mocked it


@dataclass
class StepTrace:
    step: str
    line: int
    engine: str  # the spec's engine text, e.g. "tool db read Order"
    agent: str | None = None  # the workflow agent's engine, or None when missing
    impl: str = "missing"  # ok | pending | mismatch | missing
    tests: TestUse = field(default_factory=TestUse)

    def to_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "line": self.line,
            "engine": self.engine,
            "agent": self.agent,
            "impl": self.impl,
            "tests": {"passed": self.tests.passed, "mocked": self.tests.mocked},
        }


@dataclass
class FlowTrace:
    flow: str
    line: int
    workflow: bool
    steps: list[StepTrace] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "flow": self.flow,
            "line": self.line,
            "workflow": self.workflow,
            "steps": [s.to_json() for s in self.steps],
        }


@dataclass
class VerifyResult:
    spec: str
    path: str
    findings: list[Finding] = field(default_factory=list)
    flows: list[FlowTrace] = field(default_factory=list)
    #: each tuvl-example: its generated test and that test's status
    examples: list[dict[str, Any]] = field(default_factory=list)
    #: (flow, step) pairs whose tests the spec waives (`- tests: waived — why`), with the reason
    waived_tests: dict[tuple[str, str], str] = field(default_factory=dict)
    tests_run: bool = True

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def add(self, code: str, line: int, message: str, severity: Severity = "error") -> None:
        self.findings.append(Finding(code, severity, line, message))

    def summary(self) -> dict[str, int]:
        steps = [s for f in self.flows for s in f.steps]
        return {
            "steps": len(steps),
            "implemented": sum(1 for s in steps if s.impl == "ok"),
            "mismatched": sum(1 for s in steps if s.impl == "mismatch"),
            "exercised": sum(1 for s in steps if s.tests.passed),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "path": self.path,
            "ok": not self.errors,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [
                f.to_json()
                for f in sorted(self.findings, key=lambda f: (f.severity != "error", f.line))
            ],
            "flows": [f.to_json() for f in self.flows],
            "examples": self.examples,
            "summary": self.summary(),
            "tests_run": self.tests_run,
        }


# ── comparison helpers ───────────────────────────────────────────────────────


def _type(text: str) -> TypeRef | None:
    try:
        return parse_type(text)
    except TypeParseError:
        return None


def _field_types(fields: Any) -> dict[str, TypeRef]:
    from tuvl.validate.signals import field_types

    return field_types(fields)


def _same_type(spec_type: str, impl: TypeRef | None) -> bool:
    expected = _type(spec_type)
    return expected is None or impl is None or expected == impl


def _seconds(value: str | None) -> float | None:
    return None if value is None else duration_seconds(value)


def _family(step: Step) -> tuple[str, ...]:
    """The spec engine as a comparable tuple: ("tool", "db", "read", "Order"), ("decide", "rules", "model")."""
    d = step.detail
    if step.engine == "tool":
        return tuple(d[:4]) if len(d) > 1 and d[1] in ("db", "storage") else tuple(d[:3])
    if step.engine == "decide":
        return (
            "decide",
            *(("rules",) if "rules" in d else ()),
            *(("model",) if "model" in d else ()),
        )
    if step.engine == "map":
        return ("map", d[1]) if len(d) > 1 else ("map",)
    return (step.engine,)


def _agent_family(agent: Any) -> tuple[str, ...]:
    if agent.engine == "tool":
        tool = agent.tool
        if tool.use == "db":
            return ("tool", "db", tool.op, tool.model)
        if tool.use == "http":
            return ("tool", "http", tool.method)
        if tool.use == "storage":
            return ("tool", "storage", tool.op, tool.store)
        return ("tool", "mcp", str(tool.server).removeprefix("artifact://").split("@")[0])
    if agent.engine == "map":
        return ("map", agent.map.workflow)
    if agent.engine == "decide":
        return (
            "decide",
            *(("rules",) if agent.decide.rules else ()),
            *(("model",) if agent.decide.model else ()),
        )
    return (agent.engine,)


# ── the comparison ───────────────────────────────────────────────────────────


def verify_spec(
    spec: Spec,
    text: str,
    workflows: Mapping[str, Any],
    model_definitions: Mapping[str, Mapping[str, Any]],
    tests: Mapping[str, Any] | None = None,
    results: Mapping[str, Any] | None = None,
) -> VerifyResult:
    """Compare *spec* with the project.

    *workflows*: name → WorkflowDocument. *model_definitions*: name → raw ModelDefinition ``spec``.
    *tests*: name → TestDocument; *results*: test name → TestResult (``None``: tests not run).
    """
    structure = parse_structure(text)
    out = VerifyResult(spec.name, spec.path, tests_run=results is not None)
    for model in structure.data.values():
        _verify_model(out, model, model_definitions.get(model.name))
    for flow in structure.flows.values():
        doc = workflows.get(flow.name)
        trace = FlowTrace(flow.name, flow.line, doc is not None)
        out.flows.append(trace)
        if doc is None:
            out.add(
                "R001", flow.line, f"flow {flow.name}: no workflow `{flow.name}` yet", "warning"
            )
            trace.steps = [StepTrace(s.id, s.line, " ".join(s.detail)) for s in flow.steps]
            continue
        _verify_trigger(out, flow, doc)
        _verify_ends(out, flow, doc)
        by_id = {a.id: a for a in doc.spec.agents}
        for step in flow.steps:
            trace.steps.append(_verify_step(out, flow, step, by_id.get(step.id)))
        spec_ids = {s.id for s in flow.steps}
        for agent_id in by_id.keys() - spec_ids:
            out.add(
                "R002",
                flow.line,
                f"flow {flow.name}: the workflow has agent `{agent_id}`, which the spec doesn't name",
                "warning",
            )
    out.examples = _example_status(spec, structure, tests, results)
    out.waived_tests = {
        (flow.name, step.id): step.waivers["tests"][0]
        for flow in structure.flows.values()
        for step in flow.steps
        if "tests" in step.waivers
    }
    if tests is not None and results is not None:
        _verify_tests(out, spec, structure, tests, results)
    return out


def _example_flow(spec: Spec, structure: Structure, data: Mapping[str, Any]) -> str | None:
    flows = structure.flows
    wf = data.get("workflow") or spec.front.get("workflow")
    return str(wf) if wf else (next(iter(flows)) if len(flows) == 1 else None)


def _example_status(
    spec: Spec,
    structure: Structure,
    tests: Mapping[str, Any] | None,
    results: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    out = []
    for ex in spec.examples:
        flow, name = _example_flow(spec, structure, ex.data), slug(ex.name)
        test = (tests or {}).get(name)
        failures: list[str] = []
        if test is None or test.spec.workflow != flow:
            status = "missing"
        elif results is None:
            status = "not run"
        else:
            result = results.get(name)
            status = result.status if result is not None else "not run"
            failures = list(result.failures[:3]) if result is not None else []
        out.append(
            {
                "name": ex.name,
                "line": ex.line,
                "flow": flow,
                "test": name,
                "status": status,
                "failures": failures,
            }
        )
    return out


def _verify_model(
    out: VerifyResult, model: DataModel, definition: Mapping[str, Any] | None
) -> None:
    if definition is None:
        out.add("R006", model.line, f"{model.name}: no ModelDefinition in models/ yet", "warning")
        return
    fields = {f.get("name"): f for f in definition.get("fields") or [] if isinstance(f, dict)}
    for f in model.fields.values():
        impl = fields.get(f.name)
        if impl is None:
            out.add(
                "R006", model.line, f"{model.name}.{f.name}: not a field of the ModelDefinition"
            )
            continue
        impl_type = str(impl.get("type") or "")
        if impl_type not in COLUMN_EQUIVALENTS.get(f.type, {f.type}):
            out.add(
                "R006",
                model.line,
                f"{model.name}.{f.name}: spec says {f.type}, models/ says {impl_type}",
            )
        if f.type == "enum" and f.enum and list(impl.get("enum_values") or []) != f.enum:
            out.add(
                "R006",
                model.line,
                f"{model.name}.{f.name}: enum values {impl.get('enum_values')} ≠ spec {f.enum}",
            )
        for flag in ("required", "unique", "secure"):
            if (flag in f.flags) != bool(impl.get(flag)):
                said = "is" if flag in f.flags else "is not"
                out.add(
                    "R006",
                    model.line,
                    f"{model.name}.{f.name}: the spec says it {said} {flag}; models/ disagrees",
                )
        if f.default is not None and not _same_default(f.default, impl.get("default")):
            out.add(
                "R006",
                model.line,
                f"{model.name}.{f.name}: default {impl.get('default')!r} ≠ spec {f.default!r}",
            )


def _verify_trigger(out: VerifyResult, flow: Flow, doc: Any) -> None:
    trigger, meta = doc.spec.trigger, doc.metadata
    http = trigger.http
    if flow.method:
        if http is None:
            out.add(
                "R001",
                flow.line,
                f"flow {flow.name}: spec has {flow.method} {flow.path}; the workflow has no http trigger",
            )
        elif (http.method, http.path) != (flow.method, flow.path):
            out.add(
                "R001",
                flow.line,
                f"flow {flow.name}: spec has {flow.method} {flow.path}; the workflow has {http.method} {http.path}",
            )
    elif http is not None and not flow.schedule:
        out.add(
            "R001",
            flow.line,
            f"flow {flow.name}: the workflow has an http trigger the spec doesn't declare",
        )
    cron = trigger.schedule.cron if trigger.schedule else None
    if flow.schedule and cron != flow.schedule:
        out.add(
            "R001",
            flow.line,
            f"flow {flow.name}: schedule {cron!r} ≠ spec {flow.schedule!r}",
        )
    if http is not None and flow.access:
        if flow.access == "public" and not http.public:
            out.add(
                "R001",
                flow.line,
                f"flow {flow.name}: the spec says public; the trigger is not `public: true`",
            )
        elif flow.access != "public" and http.public:
            out.add(
                "R001",
                flow.line,
                f"flow {flow.name}: the trigger is public; the spec says {flow.access}",
            )
        if flow.access.startswith("group ") and meta.required_scope != flow.access[6:]:
            out.add(
                "R001",
                flow.line,
                f"flow {flow.name}: the spec says {flow.access}; metadata.required_scope is {meta.required_scope!r}",
            )
    impl_inputs = _field_types(trigger.input)
    if set(impl_inputs) != set(flow.inputs):
        out.add(
            "R001",
            flow.line,
            f"flow {flow.name}: trigger input {sorted(impl_inputs)} ≠ spec {sorted(flow.inputs)}",
        )
    for name, t in flow.inputs.items():
        if name in impl_inputs and not _same_type(t, impl_inputs[name]):
            out.add(
                "R001",
                flow.line,
                f"flow {flow.name}: input `{name}` is {impl_inputs[name]} in the workflow, {t} in the spec",
            )


def _verify_ends(out: VerifyResult, flow: Flow, doc: Any) -> None:
    outputs = doc.spec.outputs
    for end, status in flow.ends.items():
        if end not in outputs:
            if end != "failed":  # an unmapped failure still ends the run as failed
                out.add(
                    "R005",
                    flow.line,
                    f"flow {flow.name}: end `{end}` has no output in the workflow",
                )
            continue
        if outputs[end].status != status:
            out.add(
                "R005",
                flow.line,
                f"flow {flow.name}: end `{end}` returns {outputs[end].status}, spec says {status}",
            )
    for end in outputs.keys() - flow.ends.keys():
        out.add(
            "R005",
            flow.line,
            f"flow {flow.name}: the workflow has an end `{end}` the spec doesn't list",
        )


def _verify_step(out: VerifyResult, flow: Flow, step: Step, agent: Any) -> StepTrace:
    trace = StepTrace(step.id, step.line, " ".join(step.detail))
    if agent is None:
        out.add(
            "R002",
            step.line,
            f"`{step.id}`: no agent with this id in workflow {flow.name}",
            "warning",
        )
        return trace
    trace.agent = agent.engine
    before = len(out.errors)
    if agent.engine == "pending":
        trace.impl = "pending"
        out.add("R002", step.line, f"`{step.id}`: still pending (not implemented)", "warning")
    elif _agent_family(agent) != _family(step):
        out.add(
            "R002",
            step.line,
            f"`{step.id}`: spec says {' '.join(_family(step))}, the agent is {' '.join(_agent_family(agent))}",
        )
    _verify_contract(out, step, agent)
    if step.id not in off_graph(flow):
        _verify_routes(out, flow, step, agent)
    if agent.engine != "pending":
        _verify_engine_params(out, step, agent)
    if trace.impl != "pending":
        trace.impl = "ok" if len(out.errors) == before else "mismatch"
    return trace


def _verify_contract(out: VerifyResult, step: Step, agent: Any) -> None:
    impl_in, impl_out = _field_types(agent.inputs), _field_types(agent.outputs)
    if set(impl_in) != set(step.inputs):
        out.add(
            "R003", step.line, f"`{step.id}`: inputs {sorted(impl_in)} ≠ spec {sorted(step.inputs)}"
        )
    if step.outputs and set(impl_out) != set(step.outputs):
        out.add(
            "R003",
            step.line,
            f"`{step.id}`: outputs {sorted(impl_out)} ≠ spec {sorted(step.outputs)}",
        )
    for name, t in step.outputs.items():
        if name in impl_out and not _same_type(t, impl_out[name]):
            out.add(
                "R003",
                step.line,
                f"`{step.id}`.{name}: {impl_out[name]} in the workflow, {t} in the spec",
            )


def _verify_routes(out: VerifyResult, flow: Flow, step: Step, agent: Any) -> None:
    impl_comp = getattr(agent, "compensate", None)
    if (step.compensate or None) != (impl_comp or None):
        out.add(
            "R004",
            step.line,
            f"`{step.id}`: compensate {impl_comp!r} in the workflow, {step.compensate!r} in the spec",
        )
    expected = routes_of(flow, step)
    written = {sig for r in step.routes for sig in r.signals}
    impl = dict(agent.routes)
    for sig, target in expected.items():
        if sig in step.waivers:
            continue
        got = impl.get(sig)
        if got is None:
            if sig == "error" or (sig == "default" and agent.engine == "pending"):
                continue  # unrouted error ends as failed; a pending stub may not route yet
            if sig == "default" and step.engine == "code" and sig not in written:
                continue  # a code step's fall-through is only needed if its code returns default
            out.add(
                "R004", step.line, f"`{step.id}`: route {sig} → {target} is missing in the workflow"
            )
        elif got != target:
            out.add(
                "R004",
                step.line,
                f"`{step.id}`: {sig} → {got} in the workflow, {target} in the spec",
            )
    for sig, target in impl.items():
        if sig in step.waivers:
            continue  # the spec waives this route; validate still requires the workflow to route it
        if sig not in expected and str(target).startswith("END"):
            out.add(
                "R004",
                step.line,
                f"`{step.id}`: the workflow routes {sig} → {target}, which the spec doesn't",
            )


def _verify_engine_params(out: VerifyResult, step: Step, agent: Any) -> None:
    budget = agent.budget
    if step.engine in ("llm", "loop"):
        params = agent.llm if step.engine == "llm" else agent.loop
        if step.model_ref and params.model != step.model_ref:
            out.add(
                "R007",
                step.line,
                f"`{step.id}`: model {params.model!r}, spec says {step.model_ref!r}",
            )
        if "tok" in step.budget and budget.max_tokens != int(step.budget["tok"]):
            out.add(
                "R007",
                step.line,
                f"`{step.id}`: max_tokens {budget.max_tokens}, spec says {int(step.budget['tok'])}",
            )
    if step.engine == "loop":
        if "iterations" in step.budget and budget.max_iterations != int(step.budget["iterations"]):
            out.add(
                "R007",
                step.line,
                f"`{step.id}`: max_iterations {budget.max_iterations}, spec says {int(step.budget['iterations'])}",
            )
        if "calls" in step.budget and budget.max_tool_calls != int(step.budget["calls"]):
            out.add(
                "R007",
                step.line,
                f"`{step.id}`: max_tool_calls {budget.max_tool_calls}, spec says {int(step.budget['calls'])}",
            )
        _verify_loop_tools(out, step, agent)
    if (
        step.engine in ("llm", "loop", "decide")
        and "s" in step.budget
        and _seconds(budget.timeout) != step.budget["s"]
    ):
        out.add(
            "R007",
            step.line,
            f"`{step.id}`: timeout {budget.timeout}, spec says {step.budget['s']:g}s",
        )
    if step.engine == "decide" and step.model_backed:
        model = agent.decide.model
        if model is not None:
            if step.model_ref and model.ref != step.model_ref:
                out.add(
                    "R007",
                    step.line,
                    f"`{step.id}`: decision model {model.ref!r}, spec says {step.model_ref!r}",
                )
            if step.threshold is not None and model.min_confidence != step.threshold:
                out.add(
                    "R007",
                    step.line,
                    f"`{step.id}`: min_confidence {model.min_confidence}, spec says {step.threshold}",
                )
    if step.engine == "decide" and step.rules and isinstance(agent.decide.rules, list):
        spec_rules = [(w, v) for w, v, _ in step.rules if w != "otherwise"]
        impl_rules = [(r.when, r.then) for r in agent.decide.rules]
        if [(" ".join(w.split()), v) for w, v in spec_rules] != [
            (" ".join(str(w).split()), str(v)) for w, v in impl_rules[: len(spec_rules)]
        ]:
            out.add("R007", step.line, f"`{step.id}`: the workflow's rules differ from the spec's")
    if step.engine == "human":
        human = agent.human
        if step.group and human.required_group != step.group:
            out.add(
                "R008",
                step.line,
                f"`{step.id}`: required_group {human.required_group!r}, spec says {step.group!r}",
            )
        if step.expires and _seconds(human.expires) != _seconds(step.expires):
            out.add(
                "R008", step.line, f"`{step.id}`: expires {human.expires}, spec says {step.expires}"
            )


def _verify_loop_tools(out: VerifyResult, step: Step, agent: Any) -> None:
    impl: dict[str, Any] = {}
    for tool in agent.loop.tools:
        name = (
            getattr(tool, "agent", None)
            or getattr(tool, "workflow", None)
            or (tool.tool_name if isinstance(tool, CollectionToolRef) else None)
            or str(getattr(tool, "mcp", ""))
        )
        impl[str(name)] = tool
    spec_names = {t.name for t in step.tools}
    if spec_names != {
        n.removeprefix("artifact://").split("@")[0] for n in impl
    } and spec_names != set(impl):
        out.add("R007", step.line, f"`{step.id}`: tools {sorted(impl)} ≠ spec {sorted(spec_names)}")
    for t in step.tools:
        tool = impl.get(t.name)
        if tool is None:
            continue
        approval = getattr(tool, "approval", None)
        if t.approval_group and (approval is None or approval.required_group != t.approval_group):
            out.add(
                "R007",
                step.line,
                f"`{step.id}`: tool `{t.name}` needs approval by {t.approval_group}",
            )
        if t.max_calls is not None and getattr(tool, "max_calls", None) != t.max_calls:
            out.add(
                "R007",
                step.line,
                f"`{step.id}`: tool `{t.name}` max_calls {getattr(tool, 'max_calls', None)}, spec says {t.max_calls}",
            )


def _verify_tests(
    out: VerifyResult,
    spec: Spec,
    structure: Structure,
    tests: Mapping[str, Any],
    results: Mapping[str, Any],
) -> None:
    flows = structure.flows
    for ex in spec.examples:
        wf = (
            ex.data.get("workflow")
            or spec.front.get("workflow")
            or (next(iter(flows)) if len(flows) == 1 else None)
        )
        name = slug(ex.name)
        test = tests.get(name)
        if test is None or test.spec.workflow != wf:
            out.add(
                "R009",
                ex.line,
                f"example '{ex.name}': no generated test — run `tuvl generate code`",
                "warning",
            )
            continue
        result = results.get(name)
        status = result.status if result is not None else "not run"
        if status != "passed":
            out.add(
                "R009",
                ex.line,
                f"example '{ex.name}': its test is {status}",
                "error" if status == "failed" else "warning",
            )
    for trace in out.flows:
        flow = flows[trace.flow]
        for st in trace.steps:
            for name, test in tests.items():
                result = results.get(name)
                if test.spec.workflow != trace.flow or result is None or result.status != "passed":
                    continue
                if st.step in result.path or st.step in getattr(result, "compensated", []):
                    (st.tests.mocked if st.step in test.spec.mocks else st.tests.passed).append(
                        name
                    )
            if st.step in off_graph(flow) or st.tests.passed:
                continue
            step = flow.step(st.step)
            if st.tests.mocked and step is not None and _mockable(step):
                continue  # model, external and human steps are exercised through their mocks
            if step is not None and "tests" in step.waivers:
                why, line = step.waivers["tests"]
                out.add("R010", line, f"`{st.step}`: tests waived — {why}", "waived")
                continue
            why = "only mocked" if st.tests.mocked else "no passing test runs it"
            out.add("R010", st.line, f"`{st.step}`: {why}", "warning")


def _same_default(spec_text: str, impl: Any) -> bool:
    """`default false` in the spec and `default: false` in the model are the same value."""
    if str(impl) == spec_text:
        return True
    try:
        return bool(yaml.safe_load(spec_text) == impl)
    except yaml.YAMLError:
        return False


def _mockable(step: Step) -> bool:
    """Steps tests mock by policy: non-deterministic, external or human."""
    return (
        step.engine in ("llm", "loop", "human")
        or step.model_backed
        or (step.engine == "tool" and step.detail[1:2] in (["http"], ["mcp"]))
    )


# ── the project ──────────────────────────────────────────────────────────────


def model_definitions(project: Path) -> dict[str, dict[str, Any]]:
    """Raw ``ModelDefinition`` specs in ``models/``, by name."""
    out: dict[str, dict[str, Any]] = {}
    directory = project / "models"
    for path in sorted(directory.rglob("*.y*ml")) if directory.is_dir() else []:
        for doc in yaml.safe_load_all(path.read_text()):
            if (
                isinstance(doc, dict)
                and doc.get("kind") == "ModelDefinition"
                and doc.get("enabled", True)
            ):
                name = (doc.get("metadata") or {}).get("name")
                if name:
                    out[str(name)] = doc.get("spec") or {}
    return out


async def verify_project(
    project: Path, specs: list[Path] | None = None, *, run_tests: bool = True
) -> list[VerifyResult]:
    """Verify *specs* (default: ``specs/*.md``) against the loaded project."""
    from tuvl.testing import exclusive

    async with exclusive():
        return await _verify_project(project, specs, run_tests=run_tests)


async def _verify_project(
    project: Path, specs: list[Path] | None, *, run_tests: bool
) -> list[VerifyResult]:
    from tuvl.contract.registry import TESTS, WORKFLOWS
    from tuvl.spec.parse import parse_text

    workflows, tests = dict(WORKFLOWS), dict(TESTS)

    files = (
        [p if p.is_absolute() else project / p for p in specs]
        if specs
        else sorted((project / "specs").glob("*.md"))
    )
    definitions = model_definitions(project)
    parsed = []
    for file in files:
        text = file.read_text()
        shown = str(file.relative_to(project)) if file.is_relative_to(project) else str(file)
        parsed.append((parse_text(text, path=shown, default_name=file.stem), text))
    results: dict[str, Any] | None = None
    if run_tests:
        from tuvl.testing import run_test

        flows = {f for _, text in parsed for f in parse_structure(text).flows}
        results = {}
        for name in sorted(tests):
            if tests[name].spec.workflow in flows and tests[name].spec.workflow in workflows:
                results[name] = await run_test(tests[name], workflows, base=project)
    return [
        verify_spec(spec, text, workflows, definitions, tests, results) for spec, text in parsed
    ]


async def verify_report(
    project: Path, spec: str | None = None, *, strict: bool = False, run_tests: bool = True
) -> dict[str, Any]:
    """The ``tuvl verify spec --json`` document."""
    results = await verify_project(project, [Path(spec)] if spec else None, run_tests=run_tests)
    ok = all(not r.errors and not (strict and r.warnings) for r in results)
    return {"ok": ok, "specs": [r.to_json() for r in results]}
