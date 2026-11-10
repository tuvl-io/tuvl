"""Derived task status (spec §17.5): a TaskPlan's tasks measured against the
project as it is — never a status someone typed in (``manual`` tasks aside).

=============  ===============================================================
``todo``       the target is missing, or the target agent is ``pending``
``in_progress`` the target exists but validation reports errors on it, it
               contradicts the spec (``tuvl verify spec``), or a workflow
               still has pending agents
``implemented`` the target validates cleanly and matches the spec
``tested``     a passing ``Test`` covers it and every spec example mapped to
               its workflow passes (a stubbed result doesn't count)
``done``       nothing blocks ``ship``: the lock is current and validation has
               no warnings on the target
``untracked``  the task has no target tuvl can measure
=============  ===============================================================

Overlays: ``blocked`` (the spec has blocking ``lint spec`` findings, or a
dependency is not done) and ``stale`` (the spec section changed since the task
was written). Models and artifacts skip ``tested``. Test tasks (``example``,
``assertion`` and ``judge`` targets) are ``implemented`` once the Test exists
and ``tested`` once it passes.

``tuvl generate tasks`` derives the task list; the coding agent adds only manual tasks (skill ``tuvl-plan``); :func:`plan_findings`
checks it (P001–P005, part of ``tuvl lint spec``).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from tuvl.contract.schema import Task, TaskPlanDocument, TestDocument, WorkflowDocument
from tuvl_insight.audit import live
from tuvl_insight.spec.parse import Spec

if TYPE_CHECKING:
    from tuvl_insight.spec.check import CheckResult, Finding
    from tuvl_insight.spec.grammar import Structure
    from tuvl_insight.spec.verify import VerifyResult

Status = Literal["todo", "in_progress", "implemented", "tested", "done", "untracked"]
ORDER: tuple[Status, ...] = ("todo", "in_progress", "implemented", "tested", "done")
_AGENT = re.compile(r"agents\[([a-z][a-z0-9_]*)\]")
_TASK_ID = re.compile(r"^\s*-\s+id:\s*['\"]?(T\d+|TASK-[A-Z0-9]+-\d+-\d+-\d+)", re.M)


@dataclass
class TestOutcome:
    """One test's result, as status derivation needs it."""

    __test__ = False

    document: TestDocument
    passed: bool
    path: list[str] = field(default_factory=list)


@dataclass
class ProjectState:
    workflows: dict[str, WorkflowDocument] = field(default_factory=dict)
    workflow_files: dict[str, str] = field(
        default_factory=dict
    )  # name → relative file (even if invalid)
    models: dict[str, str] = field(default_factory=dict)  # name → relative file
    artifacts: set[str] = field(default_factory=set)
    errors: dict[tuple[str, str | None], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    warnings: dict[tuple[str, str | None], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    tests: list[TestOutcome] | None = None  # None: tests were not run
    test_names: set[str] = field(default_factory=set)  # every loaded Test document
    lock_current: bool = False
    lock_note: str = ""

    def add_issue(self, severity: str, file: str, message: str) -> None:
        """File an issue under ``(workflow or model, agent?)`` by its file."""
        path = file.split(":")[0]
        owner = next(
            (n for n, f in {**self.workflow_files, **self.models}.items() if f == path), None
        )
        if owner is None:
            return
        match = _AGENT.search(message)
        key = (owner, match.group(1) if match else None)
        (self.errors if severity == "error" else self.warnings)[key].append(message)

    def issues(self, owner: str, agent: str | None, *, warnings: bool = False) -> list[str]:
        source = self.warnings if warnings else self.errors
        if agent is not None:
            return list(source.get((owner, agent), []))
        return [m for (o, _), msgs in source.items() if o == owner for m in msgs]


@dataclass
class TaskState:
    id: str
    title: str
    type: str
    status: Status
    reasons: list[str] = field(default_factory=list)
    blocked: bool = False
    stale: bool = False
    obsolete: bool = False
    target: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    waiting_on: list[str] = field(default_factory=list)
    live: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "blocked": self.blocked,
            "stale": self.stale,
            "obsolete": self.obsolete,
            "reasons": self.reasons,
            "target": self.target,
            "depends_on": self.depends_on,
            "waiting_on": self.waiting_on,
            "live": self.live,
            "failure": self.failure,
        }


def _tests_for(state: ProjectState, workflow: str) -> list[TestOutcome]:
    return [t for t in state.tests or [] if t.document.spec.workflow == workflow]


def _base(task: Task, state: ProjectState) -> tuple[Status, list[str]]:
    """todo / in_progress / implemented / untracked from the project's files and validation."""
    target = task.target
    if "model" in target:
        name = target["model"]
        if name not in state.models:
            return "todo", [f"model {name} does not exist"]
        errors = state.issues(name, None)
        return ("in_progress", errors[:3]) if errors else ("implemented", [])
    if "artifact" in target:
        name = target["artifact"]
        return (
            ("implemented", [])
            if name in state.artifacts
            else ("todo", [f"artifact {name} does not exist"])
        )
    workflow = target.get("workflow")
    if workflow is None:
        return "untracked", ["no target to measure"]
    doc = state.workflows.get(workflow)
    if doc is None:
        if workflow in state.workflow_files:
            return "in_progress", [
                f"{state.workflow_files[workflow]} does not load",
                *state.issues(workflow, None)[:2],
            ]
        return "todo", [f"workflow {workflow} does not exist"]
    if "example" in target:
        return _example_task(target, state)
    if "assertion" in target or "judge" in target or "needs" in target:
        return _test_task(target, state)
    agent_id = target.get("agent")
    if agent_id is not None:
        agent = next((a for a in doc.spec.agents if a.id == agent_id), None)
        if agent is None:
            return "todo", [f"{workflow}.{agent_id} does not exist"]
        if agent.engine == "pending":
            return "todo", [f"{workflow}.{agent_id} is pending"]
        errors = state.issues(workflow, agent_id)
        return ("in_progress", errors[:3]) if errors else ("implemented", [])
    errors = state.issues(workflow, None)
    if errors:
        return "in_progress", errors[:3]
    pending = [a.id for a in doc.spec.agents if a.engine == "pending"]
    if pending:
        return "in_progress", [f"pending agents: {', '.join(pending)}"]
    return "implemented", []


def _example_task(target: dict[str, str], state: ProjectState) -> tuple[Status, list[str]]:
    from tuvl_insight.spec.parse import slug

    name = slug(target["example"])
    if name not in state.test_names:
        return "todo", [
            f"no generated test for example '{target['example']}' — run tuvl generate code"
        ]
    if state.tests is None:
        return "implemented", ["tests not run"]
    outcome = next((t for t in state.tests if t.document.metadata.name == name), None)
    if outcome is None or not outcome.passed:
        return "implemented", [f"the test of example '{target['example']}' does not pass"]
    return "tested", []


def _test_task(target: dict[str, str], state: ProjectState) -> tuple[Status, list[str]]:
    workflow = target["workflow"]
    if state.tests is None:
        return "implemented", ["tests not run"]
    if "assertion" in target:
        # `end == 'x'` is also what a Test's structural `expect.end: x` checks
        end = re.fullmatch(r"""\s*end\s*==\s*['"]([A-Za-z0-9_.]+)['"]\s*""", target["assertion"])
        carrying = [
            t
            for t in _tests_for(state, workflow)
            if target["assertion"] in t.document.spec.expect.assertions
            or (end is not None and t.document.spec.expect.end == end.group(1).removeprefix("END."))
        ]
        what = f"assertion `{target['assertion']}`"
    else:
        wanted = target.get("judge")
        carrying = [
            t
            for t in _tests_for(state, workflow)
            if any(
                wanted in (None, j.judge.split("@")[0], j.judge)
                for j in t.document.spec.expect.judge
            )
        ]
        what = f"judge {wanted}" if wanted else "a judge expectation"
    if not carrying:
        return "todo", [f"no Test of {workflow} carries {what}"]
    if not any(t.passed for t in carrying):
        return "implemented", [f"the Test carrying {what} fails"]
    return "tested", []


def _loops_using(state: ProjectState, workflow: str, agent: str) -> set[str]:
    """Loop agents that call *agent* as a tool: a test can only reach an
    off-spine tool through its loop, so covering the loop covers the tool."""
    doc = state.workflows.get(workflow)
    if doc is None:
        return set()
    return {
        a.id
        for a in doc.spec.agents
        if a.engine == "loop"
        and any(getattr(tool, "agent", None) == agent for tool in a.loop.tools)
    }


def _tested(task: Task, state: ProjectState, spec: Spec | None) -> tuple[bool, list[str]]:
    """Whether an implemented workflow/agent target is covered by passing tests."""
    if state.tests is None:
        return False, ["tests not run (--no-tests)"]
    workflow = task.target["workflow"]
    agent = task.target.get("agent")
    tests = _tests_for(state, workflow)
    reaching = {agent} | _loops_using(state, workflow, agent) if agent else set()
    covering = [t for t in tests if t.passed and (agent is None or reaching & set(t.path))]
    if not covering:
        return False, [f"no passing Test covers {workflow}{'.' + agent if agent else ''}"]
    if spec is not None:
        from_spec = [
            t
            for t in tests
            if (t.document.metadata.generated_from or "").split("#")[0] == spec.path
        ]
        failing = [t.document.metadata.name for t in from_spec if not t.passed]
        if failing:
            return False, [f"spec examples failing: {', '.join(failing)}"]
    return True, []


def _contradictions(task: Task, verify: VerifyResult | None) -> list[str]:
    """``tuvl verify spec`` errors on the task's target: it exists but doesn't match the spec."""
    if verify is None:
        return []
    target = task.target
    if "model" in target:
        name = target["model"]
        return [
            f.message
            for f in verify.errors
            if f.code == "R006" and f.message.startswith((f"{name}.", f"{name}:"))
        ]
    flow = next((f for f in verify.flows if f.flow == target.get("workflow")), None)
    if flow is None or {"example", "assertion", "judge", "needs"} & set(target):
        return []
    agent = target.get("agent")
    if agent is not None:
        step = next((s for s in flow.steps if s.step == agent), None)
        lines = {step.line} if step is not None else set()
    else:
        lines = {flow.line, *(s.line for s in flow.steps)}
    return [f.message for f in verify.errors if f.line in lines]


def derive(
    plan: TaskPlanDocument,
    state: ProjectState,
    spec: Spec | None = None,
    *,
    check: CheckResult | None = None,
    verify: VerifyResult | None = None,
) -> list[TaskState]:
    states: dict[str, TaskState] = {}
    spec_blocked = len(check.errors) if check is not None else 0
    for task in plan.spec.tasks:
        item = TaskState(
            task.id,
            task.title,
            task.type,
            "todo",
            obsolete=task.obsolete,
            target=dict(task.target),
            depends_on=list(dict.fromkeys(task.depends_on)),
        )
        if task.obsolete:
            item.reasons = ["its step or example left the spec"]
        elif task.type == "manual":
            item.status = "todo" if task.status == "derived" else task.status
        else:
            status, reasons = _base(task, state)
            contradictions = _contradictions(task, verify) if status == "implemented" else []
            if contradictions:
                status, reasons = "in_progress", contradictions[:3]
            item.status, item.reasons = status, reasons
            measurable = {"model", "artifact"} & set(task.target)
            checks = {"example", "assertion", "judge", "needs"} & set(
                task.target
            )  # test tasks: _test_task decides
            if (
                status == "implemented"
                and not measurable
                and not checks
                and "workflow" in task.target
            ):
                ok, why = _tested(task, state, spec)
                waiver = (verify.waived_tests if verify is not None else {}).get(
                    (task.target["workflow"], task.target.get("agent", ""))
                )
                if not ok and waiver is not None:
                    ok, why = True, [f"tests waived in the spec — {waiver}"]
                if ok:
                    item.status = "tested"
                item.reasons += why
            ready = item.status == "tested" or (item.status == "implemented" and measurable)
            if ready:
                owner = (
                    None
                    if "artifact" in task.target
                    else task.target.get("model") or task.target.get("workflow")
                )
                warnings = (
                    state.issues(owner, task.target.get("agent"), warnings=True) if owner else []
                )
                if not state.lock_current:
                    note = state.lock_note or "tuvl.lock is not current"
                    item.reasons.append(
                        note if "generate lock" in note else f"{note} — run `tuvl generate lock`"
                    )
                elif warnings:
                    item.reasons += [f"warning: {w}" for w in warnings[:2]]
                else:
                    item.status = "done"
        if spec is not None and task.spec_ref is not None:
            section = spec.section(task.spec_ref.anchor)
            if section is None:
                item.stale, item.reasons = (
                    True,
                    [*item.reasons, f"spec section #{task.spec_ref.anchor} is gone"],
                )
            elif section.hash != task.spec_ref.hash:
                item.stale, item.reasons = (
                    True,
                    [*item.reasons, f"spec section #{section.anchor} changed"],
                )
        states[task.id] = item
    for task in plan.spec.tasks:
        if task.obsolete:
            continue
        if spec_blocked:
            states[task.id].blocked = True
            states[task.id].reasons.append(
                f"the spec has {spec_blocked} blocking finding(s) — tuvl lint spec"
            )
        open_deps = [
            d
            for d in dict.fromkeys(task.depends_on)
            if d in states and states[d].status != "done" and not states[d].obsolete
        ]
        if open_deps and states[task.id].status != "done":
            states[task.id].blocked = True
            states[task.id].waiting_on = open_deps
            states[task.id].reasons.append(
                "blocked: waiting on "
                + ", ".join(f"{d} {states[d].title} ({states[d].status})" for d in open_deps)
            )
    return [states[t.id] for t in plan.spec.tasks]


# ── reading the project ──────────────────────────────────────────────────────


def _names(directory: Path, project: Path, kind: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.rglob("*.y*ml")):
        try:
            for doc in yaml.safe_load_all(path.read_text()):
                if isinstance(doc, dict) and doc.get("kind") == kind:
                    name = (doc.get("metadata") or {}).get("name")
                    if name:
                        out[str(name)] = str(path.relative_to(project))
        except (OSError, yaml.YAMLError):
            continue
    return out


async def project_state(project: Path, *, run_tests: bool = True) -> ProjectState:
    """Validate, run the tests offline and check the lock (loads the project)."""
    from tuvl_insight.testing import exclusive

    async with exclusive():  # the reload below clears registries a concurrent test reads
        return await _project_state(project, run_tests=run_tests)


async def _project_state(project: Path, *, run_tests: bool) -> ProjectState:
    from tuvl.contract.registry import TESTS, WORKFLOWS
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY
    from tuvl.core.config import settings
    from tuvl.core.config_loader import load_all_configs
    from tuvl.runtime.code import load_agents
    from tuvl_insight import lock
    from tuvl_insight.cli.commands.validate import run_validation
    from tuvl_insight.testing import run_test

    result, *_ = run_validation(project, echo=False)
    load_all_configs(project)
    state = ProjectState(
        workflows=dict(WORKFLOWS),
        workflow_files=_names(settings.dir_for("workflows"), project, "Workflow"),
        models=_names(settings.dir_for("models"), project, "ModelDefinition"),
        artifacts=set(ARTIFACT_REGISTRY),
        test_names=set(TESTS),
    )
    for issue in result.issues:
        state.add_issue(issue.severity, issue.file, issue.message)
    try:
        current = lock.read(project)
        fresh = lock.build(lock.inventory(dict(WORKFLOWS), TESTS.values()))
        changed = (
            lock.differences(current, lock.merge_unlisted(fresh, current), live_mcp=False)
            if current
            else None
        )
        state.lock_current = changed == []
        state.lock_note = "tuvl.lock is missing" if current is None else "tuvl.lock is out of date"
    except lock.LockError as exc:
        state.lock_note = f"tuvl.lock cannot be built: {exc}"
    if run_tests:
        load_agents(settings.dir_for("agents"))
        state.tests = []
        for name in sorted(TESTS):
            outcome = await run_test(TESTS[name], dict(WORKFLOWS), base=project)
            reached = [
                *outcome.path,
                *outcome.compensated,
                *([outcome.waiting_at] if outcome.waiting_at else []),
            ]
            # a test that only passed through pending stubs proves nothing about the logic
            state.tests.append(TestOutcome(TESTS[name], outcome.status == "passed", reached))
    return state


class TaskPlanError(ValueError):
    """A task list that isn't a valid ``TaskPlan`` (``tuvl lint spec`` says why)."""


def plans(project: Path) -> list[tuple[Path, TaskPlanDocument]]:
    """Every ``specs/*.tasks.yaml``."""
    from pydantic import ValidationError

    directory = project / "specs"
    found = []
    for path in sorted(directory.glob("*.tasks.yaml")) if directory.is_dir() else []:
        try:
            found.append((path, TaskPlanDocument.model_validate(yaml.safe_load(path.read_text()))))
        except (ValidationError, yaml.YAMLError) as exc:
            raise TaskPlanError(
                f"{path.relative_to(project)} is not a valid task list — run `tuvl lint spec` "
                f"({str(exc).splitlines()[0]})"
            ) from exc
    return found


@dataclass
class SpecDiff:
    spec: str
    changed: bool
    stale: list[tuple[str, str]] = field(default_factory=list)  # (task id, why)
    uncovered: list[str] = field(default_factory=list)  # anchors no task points at

    def to_json(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "changed": self.changed,
            "stale": [{"task": t, "why": w} for t, w in self.stale],
            "uncovered": self.uncovered,
        }


def spec_diff(plan: TaskPlanDocument, spec: Spec) -> SpecDiff:
    """How the spec moved since the plan was made."""
    out = SpecDiff(spec.path, changed=plan.metadata.spec_hash != spec.hash)
    anchors: set[str] = set()
    for task in plan.spec.tasks:
        if task.spec_ref is None or task.obsolete:
            continue
        anchors.add(task.spec_ref.anchor)
        section = spec.section(task.spec_ref.anchor)
        if section is None:
            out.stale.append((task.id, f"section #{task.spec_ref.anchor} was removed"))
        elif section.hash != task.spec_ref.hash:
            out.stale.append((task.id, f"section #{section.anchor} changed"))
    out.uncovered = [
        s.anchor
        for s in spec.sections
        if s.level == 1
        and s.anchor not in anchors
        and not any(
            c.anchor in anchors
            for c in spec.sections
            if c.level == 2 and c.line > s.line and c.line < _next_top(spec, s)
        )
    ]
    return out


def _next_top(spec: Spec, section: Any) -> int:
    later = [s.line for s in spec.sections if s.level == 1 and s.line > section.line]
    return min(later) if later else 10**9


def selected_plans(
    project: Path, spec_file: Path | None = None
) -> list[tuple[Path, TaskPlanDocument, Spec | None]]:
    """``(tasks file, TaskPlan, parsed spec or None)`` for one spec or every plan."""
    from tuvl_insight.spec.parse import SpecError, parse_spec

    out: list[tuple[Path, TaskPlanDocument, Spec | None]] = []
    for path, plan in plans(project):
        source = project / plan.metadata.spec
        if spec_file is not None and source.resolve() != (project / spec_file).resolve():
            continue
        try:
            parsed = parse_spec(source, root=project) if source.is_file() else None
        except SpecError:
            parsed = None
        out.append((path, plan, parsed))
    return out


def derived_plans(
    project: Path, state: ProjectState, spec_file: Path | None = None
) -> list[tuple[TaskPlanDocument, list[TaskState]]]:
    """Each selected task list with its tasks' derived status (check + verify + tests)."""
    from tuvl_insight.spec.check import check_spec, project_context
    from tuvl_insight.spec.verify import model_definitions, verify_spec

    models, workflow_names = project_context()
    definitions = model_definitions(project)
    overlay, failures = live.read(project), live.read_failures(project)
    out = []
    for _, plan, parsed in selected_plans(project, spec_file):
        check = verify = None
        if parsed is not None:
            text = (project / parsed.path).read_text()
            check = check_spec(parsed, text, existing_models=models, workflows=workflow_names)
            verify = verify_spec(parsed, text, state.workflows, definitions)
        tasks = derive(plan, state, parsed, check=check, verify=verify)
        entries, failed = (
            overlay.get(plan.metadata.spec) or {},
            failures.get(plan.metadata.spec) or {},
        )
        for t in tasks:
            t.live = entries.get(t.id)
            if t.id in failed and t.status not in ("implemented", "tested", "done"):
                t.failure = failed[t.id]
                if t.live is None:
                    t.live = {"state": "failed", "finding": failed[t.id].get("summary", "")[:120]}
        out.append((plan, tasks))
    return out


def open_count(tasks: list[TaskState]) -> int:
    return sum(1 for t in tasks if not t.obsolete and (t.status != "done" or t.blocked or t.stale))


def task_detail(
    project: Path, task_id: str, spec_file: Path | None = None
) -> dict[str, Any] | None:
    """One task from its task list, with the spec section it was derived from; None when not found."""
    for _, plan, spec in selected_plans(project, spec_file):
        tasks = plan.spec.tasks
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            continue
        titles = {t.id: t.title for t in tasks}
        ref = task.spec_ref
        section = spec.section(ref.anchor) if spec and ref else None
        return {
            "id": task.id,
            "title": task.title,
            "type": task.type,
            "target": task.target,
            "obsolete": task.obsolete,
            "failure": (live.read_failures(project).get(plan.metadata.spec) or {}).get(task.id),
            "depends_on": [{"id": d, "title": titles.get(d, "")} for d in task.depends_on],
            "spec": plan.metadata.spec,
            "section": None
            if section is None or ref is None
            else {
                "anchor": section.anchor,
                "title": section.title,
                "line": section.line,
                "text": section.text,
                "changed": section.hash != ref.hash,
            },
        }
    return None


def failure_lines(failure: dict[str, Any]) -> list[str]:
    """The last failed run of a task, attempt by attempt, as plain text."""
    import time

    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(failure.get("when", 0)))
    out = [
        f"Last run failed ({when}): {failure.get('summary', '')}",
        f"  agent {failure.get('agent', '?')}, {len(failure.get('attempts', []))} of "
        f"{failure.get('max_attempts', '?')} attempt(s), ${failure.get('cost_usd', 0):.3f}",
    ]
    for a in failure.get("attempts", []):
        out.append(f"  attempt {a['attempt']} failed the {a['gate']} check:")
        out += [f"    - {p}" for p in a["problems"]]
        if a.get("output"):
            out += [
                "    agent output (end):",
                *[f"      {ln}" for ln in a["output"].splitlines()[-12:]],
            ]
    return out


def task_text(detail: dict[str, Any]) -> str:
    """A task as plain text: what it is, what it builds, what it waits for, the spec section."""
    out = [f"{detail['id']}  {detail['type']}", detail["title"], ""]
    if detail["obsolete"]:
        out += ["obsolete: its step left the spec", ""]
    if detail["target"]:
        out.append("Builds: " + ", ".join(f"{k}={v}" for k, v in detail["target"].items()))
    for dep in detail["depends_on"]:
        out.append(f"Waits for: {dep['id']} {dep['title']}".rstrip())
    failure = detail.get("failure")
    if failure:
        out += ["", *failure_lines(failure)]
    section = detail["section"]
    if section:
        edited = "  (edited since the task was derived)" if section["changed"] else ""
        out += [
            "",
            f"From the spec: {section['title']} ({detail['spec']}, line {section['line']}){edited}",
            "",
        ]
        out.append(section["text"].strip())
    return "\n".join(out)


async def status_report(
    project: Path, spec: str | None = None, *, run_tests: bool = True
) -> dict[str, Any]:
    """The ``tuvl list tasks --json`` document (also used by ``tuvl serve mcp`` and Insight)."""
    state = await project_state(project, run_tests=run_tests)
    derived = derived_plans(project, state, Path(spec) if spec else None)
    report = [
        {"spec": plan.metadata.spec, "tasks": [t.to_json() for t in tasks]}
        for plan, tasks in derived
    ]
    return {"done": bool(report) and not sum(open_count(t) for _, t in derived), "plans": report}


def diff_report(project: Path, spec: str | None = None) -> dict[str, Any]:
    """The ``tuvl show drift --json`` document."""
    return {
        "specs": [
            spec_diff(plan, parsed).to_json()
            for _, plan, parsed in selected_plans(project, Path(spec) if spec else None)
            if parsed
        ]
    }


# ── checking the task list (P001–P005, part of `tuvl lint spec`) ────────────


def plan_for(project: Path, spec: Spec) -> Path | None:
    """The task list of *spec*: the ``*.tasks.yaml`` naming it, else ``<spec>.tasks.yaml``."""
    directory = project / "specs"
    for path in sorted(directory.glob("*.tasks.yaml")) if directory.is_dir() else []:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            continue
        if isinstance(raw, dict) and (raw.get("metadata") or {}).get("spec") == spec.path:
            return path
    default = (project / spec.path).with_suffix(".tasks.yaml")
    return default if default.is_file() else None


def plan_findings(project: Path, spec: Spec, structure: Structure) -> list[Finding]:
    """P001–P005 for *spec*'s task list (written by the coding agent, skill ``tuvl-plan``)."""
    from pydantic import ValidationError

    from tuvl_insight.spec.check import Finding

    path = plan_for(project, spec)
    if path is None:
        return [
            Finding(
                "P003",
                "warning",
                1,
                f"no task list yet — `tuvl generate tasks` derives {Path(spec.path).with_suffix('.tasks.yaml')} "
                "from the approved spec",
            )
        ]
    shown = str(path.relative_to(project))
    text = path.read_text()
    out: list[Finding] = []

    def add(code: str, line: int, message: str, severity: str = "error") -> None:
        out.append(Finding(code, severity, line, message, file=shown))  # type: ignore[arg-type]

    lines = {m.group(1): text[: m.start()].count("\n") + 1 for m in _TASK_ID.finditer(text)}
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        add("P001", 1, f"not YAML: {' '.join(str(exc).split())[:160]}")
        return out
    for task in ((raw.get("spec") or {}).get("tasks") or []) if isinstance(raw, dict) else []:
        if not isinstance(task, dict):
            continue
        line = lines.get(str(task.get("id")), 1)
        if "effort" in task:
            add(
                "P004",
                line,
                f"{task.get('id')}: carries an estimate — tuvl tracks status, not effort; remove `effort`",
            )
            task.pop("effort")
        if task.get("type") != "manual" and task.get("status", "derived") != "derived":
            add(
                "P004",
                line,
                f"{task.get('id')}: a hand-set status — only `manual` tasks set one; it is derived",
            )
            task.pop("status")
    try:
        plan = TaskPlanDocument.model_validate(raw)
    except ValidationError as exc:
        for error in exc.errors()[:5]:
            where = ".".join(str(p) for p in error["loc"])
            add("P001", 1, f"not a TaskPlan: {where}: {error['msg']}")
        return out
    flows = structure.flows
    examples = {ex.name for ex in spec.examples}
    targeted: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for task in plan.spec.tasks:
        line, target = lines.get(task.id, 1), task.target
        if not target and task.type != "manual":
            add(
                "P001",
                line,
                f"{task.id}: no target — point it at a model, workflow + agent or example (or make it manual)",
            )
        if task.obsolete:
            continue
        if task.spec_ref is not None:
            section = spec.section(task.spec_ref.anchor)
            if section is None:
                add(
                    "P005",
                    line,
                    f"{task.id}: spec section #{task.spec_ref.anchor} is gone",
                    "warning",
                )
            elif section.hash != task.spec_ref.hash:
                add(
                    "P005",
                    line,
                    f"{task.id}: spec section #{section.anchor} changed since the task was written",
                    "warning",
                )
        if "model" in target:
            if target["model"] not in structure.data:
                add("P002", line, f"{task.id}: model {target['model']} is not in the spec's # Data")
            targeted["model"].add((target["model"],))
        workflow = target.get("workflow")
        if workflow is None:
            continue
        flow = flows.get(workflow)
        if flow is None:
            add("P002", line, f"{task.id}: workflow {workflow} is not a `## Flow:` of the spec")
            continue
        if "agent" in target:
            if flow.step(target["agent"]) is None:
                add(
                    "P002",
                    line,
                    f"{task.id}: {workflow} has no step `{target['agent']}` in the spec",
                )
            kind = "integration" if task.type in ("integration", "manual") else "step"
            targeted[kind].add((workflow, target["agent"]))
        if "example" in target:
            if target["example"] not in examples:
                add("P002", line, f"{task.id}: the spec has no example named '{target['example']}'")
            targeted["example"].add((target["example"],))
    for model in structure.data.values():
        if model.new and (model.name,) not in targeted["model"]:
            add("P003", model.line, f"model {model.name} has no task", "warning")
    for flow in flows.values():
        for step in flow.steps:
            if (flow.name, step.id) not in targeted["step"]:
                add("P003", step.line, f"step `{step.id}` ({flow.name}) has no task", "warning")
            external = step.engine == "tool" and step.detail[1:2] in (["http"], ["mcp"])
            if external and (flow.name, step.id) not in targeted["integration"]:
                add(
                    "P003",
                    step.line,
                    f"step `{step.id}` calls an external system — add a manual task for its credentials",
                    "warning",
                )
    for ex in spec.examples:
        if (ex.name,) not in targeted["example"]:
            add("P003", ex.line, f"example '{ex.name}' has no task", "warning")
    return out
