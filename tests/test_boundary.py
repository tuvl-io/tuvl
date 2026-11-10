"""The line between the engine (`tuvl`) and the developer tooling (`tuvl_insight`).

`scripts/split_report.py` reads every import in both packages. Three rules, all about edges that cross the line:

1. The engine imports the tooling only at the listed seams (`SEAMS` in the script).
2. The tooling imports only the engine modules listed in `ENGINE_SURFACE` below, each with the reason it needs
   it. A new entry needs a reviewer's eyes: prefer a module already on the list, or put the capability behind
   `tuvl.runtime.host`. An entry nobody uses any more must be removed, so the list stays the real surface.
3. The tooling does not import an underscore name from the engine: if it needs it, it is public.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent

#: engine module -> why the tooling needs it
ENGINE_SURFACE: dict[str, str] = {
    # ── the contract: the language both sides speak ──
    "tuvl.contract.schema": "the document models the tooling reads, writes and validates",
    "tuvl.contract.registry": "the loaded workflows/tests, which lint, spec checks and the canvas read",
    "tuvl.contract.types": "the field type grammar: type checks, code generation, UI specs",
    "tuvl.contract.tel": "the expression language: expression checks and spec checks",
    "tuvl.contract.signals": "reserved signal names and HTTP status signals for routing checks",
    "tuvl.contract.facts": "workflow facts shared with the runtime: field types, effects, artifact references",
    "tuvl.contract.surface": "the API surface of a workflow (OpenAPI, UI specs)",
    "tuvl.contract.files": "the project file kinds and layout",
    "tuvl.contract.lockfile": "reading tuvl.lock when verifying it",
    # ── loading a project: the tooling checks what the engine would load ──
    "tuvl.core.config": "settings and the project directory",
    "tuvl.core.config_loader": "load and walk the project's YAML exactly as the engine does (lint, tests, specs)",
    "tuvl.core.system_config": ".tuvl/system.yaml: the API surface settings the dev API edits",
    "tuvl.core.project_info": "the project record (id, name, registry) shown and edited by Insight",
    "tuvl.core.logger": "structured logging",
    "tuvl.core.dev.project": "the project directory the dev API works on",
    "tuvl.core.artifacts.kinds": "artifact types and size limits, for artifact lint and the editor",
    "tuvl.core.artifacts.loader": "resolve artifact sources the way the engine does",
    "tuvl.core.artifacts.registry": "the registered artifacts, listed and checked by lint and the editor",
    "tuvl.core.models.agent_models": "AgentModel registry: model requirements and code assist",
    "tuvl.core.models.collections_loader": "collection names, for lint of search/store steps",
    "tuvl.core.models.loader": "model registry, for lint, code generation and code assist",
    "tuvl.core.models.requirements": "which environment variables a project's models need",
    "tuvl.core.models.schemas": "generated request/response schemas (code generation, UI specs)",
    # ── the engine's own vocabulary, so lint rejects exactly what the engine would ──
    "tuvl.core.auth.schemas": "the scope-name pattern, for scope lint",
    "tuvl.core.auth.seed": "the `kind: RoleSeed` parser, for role-seed lint",
    "tuvl.core.envconfig": "`kind: EnvConfig`: its parser, the engine variable catalog and the manifest that lint, ship and the env dev API check against",
    "tuvl.core.bootstrap": "the valid deployment modes, for system.yaml lint",
    "tuvl.core.engine.guardrails": "the guardrail checks that exist, for guardrail lint",
    "tuvl.core.engine.hooks": "the hook events and actions that exist, for hook lint",
    "tuvl.runtime.builtins": "the built-in data/memory step names and params, for tool lint",
    "tuvl.runtime.decision": "the decision-model providers that exist, for decide lint",
    "tuvl.storage": "parse a storage declaration, for lint and ship",
    "tuvl.storage.keys": "the object-key rules, for storage step lint",
    "tuvl.storage.spec": "the declared stores, for lint",
    "tuvl.webhooks.guard": "the SSRF guard for URLs the tooling fetches (idea import)",
    "tuvl.llm": "the LLM client, used by code assist",
    "tuvl.extras": "the production extras, which `ship` pins",
    # ── running things for the developer ──
    "tuvl.runtime.code": "load the project's Python agents, to lint, test, replay and show them",
    "tuvl.runtime.tool": "open an MCP session to list a server's tools when generating the lock",
    "tuvl.runtime.host": "the running host: the canvas, probes and replay talk to it",
    "tuvl.runtime.journal": "read a run's journal for pin and replay",
    "tuvl.runtime.queue": "the work queue the in-process test runner drives",
    "tuvl.runtime.models": "runtime tables and run records, for the in-process test runner",
    "tuvl.runtime.engines": "an engine's result type, which the test runner's stand-ins return",
    "tuvl.runtime.runner": "the test runner is a Runner subclass that serves mocks and replays",
    "tuvl.runtime.judges": "judge execution, for judge probes in the editor and `run tests`",
    "tuvl.api.triggers": "the trigger operation (path, schema), for the UI spec",
    "tuvl.api.uploads": "the file inputs of a trigger, for the UI spec",
    # ── presentation and registration ──
    "tuvl.cli.logo": "the banner the terminal UI shows",
    "tuvl.doctor": "register the toolchain's check groups",
    "tuvl": "the package version",
}


def _report() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "split_report", ROOT / "scripts" / "split_report.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_engine_imports_the_tooling_only_at_the_seams() -> None:
    bad = _report().seam_violations()
    assert not bad, (
        "these engine modules import developer tooling; move the shared code into the contract/core, "
        "or (if it is a real seam) list it in scripts/split_report.py SEAMS with a reason:\n"
        + "\n".join(f"  {module} -> {target}" for module, target in bad)
    )


def test_the_tooling_imports_only_the_listed_engine_modules() -> None:
    used = set(_report().core_used_by_tooling())
    new = sorted(used - set(ENGINE_SURFACE))
    assert not new, (
        f"the tooling now imports engine modules that are not listed: {new}. Prefer a module already on "
        "the list or put the capability behind tuvl.runtime.host; otherwise add it to ENGINE_SURFACE with its reason."
    )
    stale = sorted(set(ENGINE_SURFACE) - used)
    assert not stale, f"no longer imported by the tooling, remove from ENGINE_SURFACE: {stale}"


def test_the_tooling_does_not_import_private_engine_names() -> None:
    bad = _report().private_imports()
    assert not bad, (
        "make these names public in the engine (they are part of its surface now):\n"
        + "\n".join(f"  {module} imports {name}" for module, name in bad)
    )


def test_listing_a_project_in_the_nest_is_developer_toolchain_only() -> None:
    """The Nest client, the challenge-tag push and the register routes live in `tuvl_insight`; the engine has
    no code that talks to a Nest or pushes to a project's git remote."""
    needles = ("/api/register-project", "/api/verify-project", "tuvl-nest-verify", "nest.tuvl.io")
    leaks = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src" / "tuvl").rglob("*.py")
        if any(needle in path.read_text() for needle in needles)
    ]
    assert not leaks, f"Nest registration code in the engine: {leaks}"
