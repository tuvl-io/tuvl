"""The structured parts of a spec (proposals/spec-referee.md §5): ``# Data`` and ``# Flows``.

Line-based and deterministic. Prose sections are not read. Anything inside Data or Flows that
doesn't parse becomes an S001 finding — a line is never skipped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COLUMN_TYPES = frozenset(
    {
        "string",
        "text",
        "integer",
        "numeric",
        "boolean",
        "uuid",
        "date",
        "timestamptz",
        "jsonb",
        "enum",
    }
)
FIELD_FLAGS = frozenset({"required", "unique", "secure"})
ENGINES = frozenset({"code", "tool", "decide", "llm", "loop", "human", "map", "parallel"})

_FIELD = re.compile(r"`(\w+)` \(([^()]*)\)")
_FIELD_OPT = re.compile(r"`(\w+)`(\?)? \(([^()]*)\)")
_NAME = re.compile(r"`(\w+)`")
_INPUT = re.compile(r"`(\w+)`(\?)?")
_TOOL = re.compile(r"^(?:(workflow|mcp) )?`([\w.-]+)`(?: \((.+)\))?$")
_APPROVAL = re.compile(r"^approval (\w+)(?: when `([^`]+)`)?$")
_STEP = re.compile(r"^(T?\d+)\. `(\w+)` — (.+)$")
_SUB = re.compile(r"^\s+- (.+)$")
_CONT = re.compile(r"^\s{2,}· (.+)$")
_RULE = re.compile(r"^`([^`]+)` → (\w+)$")
_OTHERWISE = re.compile(r"^otherwise → (\w+)$")
_WAIVER = re.compile(r"^(\w+): waived — (.+)$")
_NO_EXAMPLE = re.compile(r"^(\w+): no example — (.+)$")
_ROUTE = re.compile(r"^([\w, ]+?)(?: \(out (.+)\))? → (END(?:\.\w+)?|`\w+`)$")
_MODEL = re.compile(r"^- \*\*(\w+)\*\* \((new|existing)\)(?: — (.*))?$")
_ENDS = re.compile(r"^\s*(\w+) (\d{3})\s*$")
_THRESHOLD = re.compile(r"(?:≥|>=)\s*(0?\.\d+|1(?:\.0+)?)")


@dataclass
class Problem:
    line: int
    message: str


@dataclass
class DataField:
    name: str
    type: str
    enum: list[str] = field(default_factory=list)
    ref: str | None = None
    flags: set[str] = field(default_factory=set)
    default: str | None = None


@dataclass
class DataModel:
    name: str
    new: bool
    line: int
    fields: dict[str, DataField] = field(default_factory=dict)


@dataclass
class Route:
    signals: list[str]
    target: str  # step id, "END" or "END.<name>"
    outputs: dict[str, str] = field(default_factory=dict)
    line: int = 0


@dataclass
class Tool:
    kind: str  # "step" | "workflow" | "mcp"
    name: str
    approval_group: str | None = None
    approval_when: str | None = None
    max_calls: int | None = None


@dataclass
class Step:
    id: str
    number: str
    line: int
    engine: str
    detail: list[str]  # the engine part, split on spaces: ["tool", "db", "read", "Order"]
    inputs: list[str] = field(default_factory=list)
    optional_inputs: set[str] = field(default_factory=set)  # `x`? — may be absent
    outputs: dict[str, str] = field(default_factory=dict)
    budget: dict[str, float] = field(default_factory=dict)  # tok, s, iterations, calls, retry
    guardrails: list[str] = field(default_factory=list)
    effect: str | None = None
    expires: str | None = None
    group: str | None = None
    threshold: float | None = None
    tools: list[Tool] = field(default_factory=list)
    tool_of: str | None = None
    compensate: str | None = None  # the T step that undoes this one on a rollback end
    routes: list[Route] = field(default_factory=list)
    rules: list[tuple[str, str, int]] = field(
        default_factory=list
    )  # (TEL | "otherwise", value, line)
    waivers: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def off_spine(self) -> bool:
        return self.number.startswith("T")

    @property
    def model_backed(self) -> bool:
        return self.engine in ("llm", "loop") or (
            self.engine == "decide" and "model" in self.detail
        )

    @property
    def model_ref(self) -> str | None:
        if self.engine in ("llm", "loop") and len(self.detail) > 1:
            return self.detail[1]
        if self.engine == "decide" and "model" in self.detail:
            i = self.detail.index("model")
            return self.detail[i + 1] if i + 1 < len(self.detail) else None
        return None


@dataclass
class Flow:
    name: str
    line: int
    method: str | None = None
    path: str | None = None
    schedule: str | None = None
    tool_only: bool = False  # `Trigger: tool` — only called as a loop's workflow tool
    access: str | None = None  # "signed-in" | "public" | "group <g>"
    inputs: dict[str, str] = field(default_factory=dict)
    ends: dict[str, int] = field(default_factory=dict)
    no_example: dict[str, tuple[str, int]] = field(default_factory=dict)
    policy: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    has_trigger: bool = False

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


@dataclass
class Structure:
    data: dict[str, DataModel] = field(default_factory=dict)
    flows: dict[str, Flow] = field(default_factory=dict)
    prose: list[str] = field(default_factory=list)  # top-level sections that are not checked
    acceptance: list[int] = field(default_factory=list)  # free-text bullets under # Acceptance
    problems: list[Problem] = field(default_factory=list)
    has_data: bool = False
    has_flows: bool = False


def parse_structure(text: str) -> Structure:
    """Parse the ``# Data`` and ``# Flows`` sections of a spec's Markdown."""
    out = Structure()
    section: str | None = None
    flow: Flow | None = None
    data_lines: list[tuple[int, str]] = []
    fence: str | None = None
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":  # front matter
        start = next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
    for n, line in enumerate(lines[start:], start + 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        if line.startswith("# "):
            section, flow = line[2:].strip().lower(), None
            if section == "data":
                out.has_data = True
            elif section == "flows":
                out.has_flows = True
            elif section != "acceptance":
                out.prose.append(section)
            continue
        if not stripped:
            continue
        if section == "acceptance" and line.startswith("- "):
            out.acceptance.append(n)
        elif section == "data":
            data_lines.append((n, line))
        elif section == "flows":
            heading = re.match(r"^## Flow: (\w+)\s*$", line)
            if heading:
                flow = Flow(heading.group(1), n)
                if flow.name in out.flows:
                    out.problems.append(Problem(n, f"flow `{flow.name}` is declared twice"))
                out.flows[flow.name] = flow
            elif line.startswith("## "):
                flow = None  # a prose sub-section of #Flows
            elif flow is not None:
                _flow_line(out, flow, n, line)
            else:
                out.problems.append(
                    Problem(n, "text in #Flows before the first `## Flow:` heading")
                )
    for n, line in _join_continuations(data_lines):
        _data_line(out, n, line)
    return out


def _join_continuations(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    joined: list[tuple[int, str]] = []
    for n, text in lines:
        if joined and text.startswith("  ") and not text.lstrip().startswith("- "):
            joined[-1] = (joined[-1][0], joined[-1][1] + " " + text.strip())
        else:
            joined.append((n, text))
    return joined


def _data_line(out: Structure, n: int, line: str) -> None:
    m = _MODEL.match(line)
    if not m:
        out.problems.append(Problem(n, f"Data line doesn't parse: {line.strip()[:70]}"))
        return
    model = DataModel(m.group(1), m.group(2) == "new", n)
    out.data[model.name] = model
    for name, inside in _FIELD.findall(m.group(3) or ""):
        parts = [p.strip() for p in inside.split(";")]
        head, flags = parts[0], parts[1:]
        f = DataField(name, head)
        if head.startswith("enum:"):
            f.type, f.enum = "enum", [v.strip() for v in head[5:].split("|") if v.strip()]
        elif "→" in head:
            f.type, f.ref = (s.strip() for s in head.split("→", 1))
        for flag in flags:
            if flag in FIELD_FLAGS:
                f.flags.add(flag)
            elif flag.startswith("default "):
                f.default = flag[8:].strip()
            else:
                out.problems.append(Problem(n, f"{model.name}.{name}: unknown flag '{flag}'"))
        model.fields[name] = f


def _fields(text: str) -> dict[str, str]:
    return {
        name: t if not optional or t.endswith("?") else f"{t}?"
        for name, optional, t in _FIELD_OPT.findall(text)
    }


def _budget(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (p.strip() for p in text.split(",")):
        m = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*(tok|s|m|h|iterations|tool calls)", part
        ) or re.fullmatch(r"(retry) (\d+)", part)
        if not m:
            raise ValueError(part)
        if m.group(1) == "retry":
            out["retry"] = float(m.group(2))
            continue
        amount, unit = float(m.group(1)), m.group(2)
        key = {"m": "s", "h": "s", "tool calls": "calls"}.get(unit, unit)
        out[key] = amount * {"m": 60, "h": 3600}.get(unit, 1)
    return out


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split on *sep* outside parentheses and backticks."""
    parts, depth, tick, buf = [], 0, False, ""
    for ch in text:
        if ch == "`":
            tick = not tick
        elif not tick and ch == "(":
            depth += 1
        elif not tick and ch == ")":
            depth -= 1
        if ch == sep and depth == 0 and not tick:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    return [*parts, buf.strip()] if buf.strip() else parts


def _flow_line(out: Structure, flow: Flow, n: int, line: str) -> None:
    if line.startswith("Trigger:"):
        flow.has_trigger = True
        for part in (p.strip() for p in line[8:].split("·")):
            m = re.match(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$", part)
            if m:
                flow.method, flow.path = m.group(1), m.group(2)
            elif part.startswith("schedule "):
                flow.schedule = part[9:].strip()
            elif part == "tool":
                flow.tool_only = True
            elif part in ("signed-in", "public") or part.startswith("group "):
                flow.access = part
            else:
                out.problems.append(Problem(n, f"`Trigger:` part doesn't parse: '{part}'"))
        return
    if line.startswith("Input:"):
        flow.inputs = _fields(line)
        for name in _NAME.findall(line):
            if name not in flow.inputs:
                out.problems.append(
                    Problem(
                        n,
                        f"flow {flow.name}: input `{name}` doesn't parse: write `{name}` (type), "
                        f"or `{name}`? (type) when it is optional",
                    )
                )
        return
    if line.startswith("Ends:"):
        for part in line[5:].split("·"):
            m = _ENDS.match(part)
            if m:
                flow.ends[m.group(1)] = int(m.group(2))
            else:
                out.problems.append(Problem(n, f"`Ends:` entry doesn't parse: '{part.strip()}'"))
        return
    if line.startswith("Policy:"):
        flow.policy = [p.strip() for p in line[7:].split("·") if p.strip()]
        return
    m = _STEP.match(line)
    if m:
        step = Step(m.group(2), m.group(1), n, "", [])
        _step_parts(out, step, n, [p.strip() for p in m.group(3).split(" · ")])
        if flow.step(step.id):
            out.problems.append(Problem(n, f"step `{step.id}` is declared twice"))
        flow.steps.append(step)
        return
    cont = _CONT.match(line)
    if cont and flow.steps:
        _step_parts(
            out, flow.steps[-1], n, [p.strip() for p in cont.group(1).split(" · ")], more=True
        )
        return
    sub = _SUB.match(line)
    if sub:
        item = sub.group(1).strip()
        if not flow.steps:
            ack = _NO_EXAMPLE.match(item)
            if ack:
                flow.no_example[ack.group(1)] = (ack.group(2), n)
            else:
                out.problems.append(Problem(n, f"line doesn't parse: {item[:70]}"))
            return
        _step_item(out, flow.steps[-1], n, item)
        return
    out.problems.append(Problem(n, f"line doesn't parse: {line.strip()[:70]}"))


def _step_parts(
    out: Structure, step: Step, n: int, parts: list[str], *, more: bool = False
) -> None:
    if not more:
        step.detail = parts[0].split()
        step.engine = step.detail[0]
        if step.engine not in ENGINES:
            out.problems.append(Problem(n, f"`{step.id}`: unknown engine '{step.engine}'"))
        if step.engine == "map" and len(step.detail) < 2:
            out.problems.append(
                Problem(n, f"`{step.id}`: a map step names the workflow it runs: `map <workflow>`")
            )
        if step.engine == "decide":
            t = _THRESHOLD.search(parts[0])
            step.threshold = float(t.group(1)) if t else None
        if step.engine == "human" and len(step.detail) > 2 and step.detail[1] == "group":
            step.group = step.detail[2]
        parts = parts[1:]
    for part in parts:
        if part.startswith("in "):
            for name, optional in _INPUT.findall(part):
                step.inputs.append(name)
                if optional:
                    step.optional_inputs.add(name)
        elif part.startswith("out "):
            step.outputs.update(_fields(part))
        elif part.startswith("budget "):
            try:
                step.budget.update(_budget(part[7:]))
            except ValueError as exc:
                out.problems.append(Problem(n, f"`{step.id}`: budget part doesn't parse: '{exc}'"))
        elif part.startswith("guardrails "):
            step.guardrails += re.findall(r"`([\w.@/:-]+)`", part)
        elif part.startswith("effect "):
            step.effect = part[7:].strip()
        elif part.startswith("expires "):
            step.expires = part[8:].strip()
        elif part.startswith("tools: "):
            for token in _split_top(part[7:]):
                m = _TOOL.match(token)
                if not m:
                    out.problems.append(Problem(n, f"`{step.id}`: tool doesn't parse: '{token}'"))
                    continue
                tool = Tool(m.group(1) or "step", m.group(2))
                for q in _split_top(m.group(3) or "", ";"):
                    if a := _APPROVAL.match(q):
                        tool.approval_group, tool.approval_when = a.group(1), a.group(2)
                    elif c := re.fullmatch(r"max (\d+) calls?", q):
                        tool.max_calls = int(c.group(1))
                    else:
                        out.problems.append(
                            Problem(n, f"`{step.id}`: tool qualifier doesn't parse: '{q}'")
                        )
                step.tools.append(tool)
        elif part.startswith("compensate "):
            m = _NAME.search(part)
            step.compensate = m.group(1) if m else None
            if m is None:
                out.problems.append(
                    Problem(n, f"`{step.id}`: compensate part doesn't parse: '{part}'")
                )
        elif part.startswith("tool of "):
            m = _NAME.search(part)
            step.tool_of = m.group(1) if m else None
        else:
            out.problems.append(Problem(n, f"`{step.id}`: unknown part '{part}'"))


def _step_item(out: Structure, step: Step, n: int, item: str) -> None:
    if r := _RULE.match(item):
        step.rules.append((r.group(1), r.group(2), n))
    elif r := _OTHERWISE.match(item):
        step.rules.append(("otherwise", r.group(1), n))
    elif w := _WAIVER.match(item):
        step.waivers[w.group(1)] = (w.group(2), n)
    elif r := _ROUTE.match(item):
        step.routes.append(
            Route(
                [s.strip() for s in r.group(1).split(",")],
                r.group(3).strip("`"),
                _fields(r.group(2) or ""),
                n,
            )
        )
    else:
        out.problems.append(Problem(n, f"`{step.id}`: line doesn't parse: {item[:70]}"))
