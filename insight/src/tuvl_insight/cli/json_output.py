"""The documented ``--json`` outputs of the CLI (spec §20): one Pydantic model
per command, exported with ``tuvl export schema --kind cli`` and
snapshot-tested, so coding agents and CI can rely on the shapes.

Fields may be added in a minor release; none are removed or retyped.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Issue(_Out):
    severity: Literal["error", "warning"]
    file: str
    line: int | None
    code: str | None
    message: str


class ValidateReport(_Out):
    """``tuvl lint project --json``"""

    valid: bool
    errors: list[Issue]
    warnings: list[Issue]
    profiles: dict[str, dict[str, Any]]
    #: Models the workflows use: provider, model, env variables, problems, users (V028/V029).
    models: dict[str, dict[str, Any]] = {}


class DoctorCheck(_Out):
    group: str
    id: str
    status: Literal["ok", "warn", "fail", "skip"]
    detail: str
    hint: str
    #: ``tuvl check system --fix`` can repair it
    fixable: bool


class DoctorSummary(_Out):
    ok: int
    warn: int
    fail: int
    skip: int


class DoctorReport(_Out):
    """``tuvl check system --json``"""

    healthy: bool
    models: dict[str, dict[str, Any]]
    #: ``kind: Storage`` stores: provider and the environment variables still missing.
    storage: dict[str, dict[str, Any]] = {}
    #: the check groups that ran
    groups: list[str] = []
    summary: DoctorSummary | None = None
    checks: list[DoctorCheck] = []
    #: labels of the fixes ``--fix`` applied
    fixed: list[str] = []


class StorageCheckReport(_Out):
    """``tuvl check storage --json``: per store ``ok``, ``provider`` and either ``missing`` or ``steps``."""

    healthy: bool
    stores: dict[str, dict[str, Any]]


class SpecFinding(_Out):
    code: str
    severity: Literal["error", "warning", "waived"]
    line: int
    message: str
    #: set when the finding is in the task list rather than the spec
    file: str | None = None


class SpecCheck(_Out):
    spec: str
    path: str
    ok: bool
    errors: int
    warnings: int
    findings: list[SpecFinding]
    #: prose sections, read by people and the coding agent but not checked
    unchecked: list[str]
    #: the spec's hash and its sections' anchors and hashes (a task list's spec_hash / spec_ref)
    hash: str
    sections: list[dict[str, Any]]


class SpecCheckReport(_Out):
    """``tuvl lint spec --json``"""

    ok: bool
    specs: list[SpecCheck]


class StepTests(_Out):
    passed: list[str]
    mocked: list[str]


class StepTrace(_Out):
    step: str
    line: int
    engine: str
    agent: str | None
    impl: Literal["ok", "pending", "mismatch", "missing"]
    tests: StepTests


class FlowTrace(_Out):
    flow: str
    line: int
    workflow: bool
    steps: list[StepTrace]


class ExampleTrace(_Out):
    name: str
    line: int
    flow: str | None
    test: str
    status: Literal["passed", "stubbed", "failed", "missing", "not run"]
    #: why a failed test failed (up to three)
    failures: list[str] = []


class SpecVerify(_Out):
    spec: str
    path: str
    ok: bool
    errors: int
    warnings: int
    findings: list[SpecFinding]
    #: spec step → workflow agent → tests (the traceability matrix)
    flows: list[FlowTrace]
    #: each tuvl-example and its generated test's status
    examples: list[ExampleTrace]
    summary: dict[str, int]
    tests_run: bool


class SpecVerifyReport(_Out):
    """``tuvl verify spec --json``"""

    ok: bool
    specs: list[SpecVerify]


class TestResult(_Out):
    __test__ = False

    name: str
    workflow: str
    #: failed · stubbed (expectations met through pending placeholders — not counted) · passed
    status: Literal["passed", "stubbed", "failed"]
    passed: bool
    failures: list[str]
    end: str | None
    path: list[str]
    tokens: int
    stubbed: list[str]
    warnings: list[str]
    #: compensators that ran (and succeeded) on a rollback end
    compensated: list[str] = []


class TestReport(_Out):
    """``tuvl run tests --json``"""

    __test__ = False

    passed: bool
    tests: list[TestResult]
    warnings: list[str]
    #: Names of stubbed tests (dev API / MCP; the CLI reports it per test).
    stubbed: list[str] = []


class ReplayReport(_Out):
    """``tuvl replay runs --json``"""

    passed: bool
    replays: list[TestResult]


class TaskStatus(_Out):
    id: str
    title: str
    type: str
    status: Literal["todo", "in_progress", "implemented", "tested", "done", "untracked"]
    blocked: bool
    stale: bool
    obsolete: bool
    reasons: list[str]
    target: dict[str, str] = {}
    depends_on: list[str] = []
    waiting_on: list[str] = []
    live: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None


class PlanStatus(_Out):
    spec: str
    tasks: list[TaskStatus]


class SpecStatusReport(_Out):
    """``tuvl list tasks --json``"""

    done: bool
    plans: list[PlanStatus]


class StaleTask(_Out):
    task: str
    why: str


class SpecDiff(_Out):
    spec: str
    changed: bool
    stale: list[StaleTask]
    uncovered: list[str]


class SpecDiffReport(_Out):
    """``tuvl show drift --json`` (a list, one entry per spec)"""

    specs: list[SpecDiff]


COMMANDS: dict[str, type[BaseModel]] = {
    "lint project": ValidateReport,
    "check system": DoctorReport,
    "check storage": StorageCheckReport,
    "lint spec": SpecCheckReport,
    "verify spec": SpecVerifyReport,
    "run tests": TestReport,
    "replay runs": ReplayReport,
    "list tasks": SpecStatusReport,
    "show drift": SpecDiffReport,
}


def json_schemas() -> dict[str, Any]:
    return {name: model.model_json_schema() for name, model in COMMANDS.items()}
