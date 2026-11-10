"""Expressions, templates and artifact references (V008, V017)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from tuvl.contract.facts import artifact_refs, field_types
from tuvl.contract.schema import (
    AgentToolRef,
    CodeAgent,
    CollectionToolRef,
    DecideAgent,
    HumanAgent,
    LlmAgent,
    LoopAgent,
    MapAgent,
    ParallelAgent,
    ToolAgent,
    WorkflowToolRef,
)
from tuvl.contract.tel import TELError, compile_expr, compile_template
from tuvl.contract.types import TypeRef, build_model
from tuvl.core.artifacts.kinds import REF_SITE_COMPAT
from tuvl_insight.validate.diagnostics import Loc
from tuvl_insight.validate.workflow.base import _Base
from tuvl_insight.validate.workflow.helpers import _strings, _tel_message


class _Expressions(_Base):
    def check_expressions(self, agent: Any) -> None:
        env: dict[str, Any] = dict(agent.inputs)
        for loc, text in self.templates_of(agent):
            self.check_template(text, env, self.loc(agent.id, *loc), "V008")
        if isinstance(agent, DecideAgent) and isinstance(agent.decide.rules, list):
            for k, rule in enumerate(agent.decide.rules):
                self.check_condition(
                    rule.when, env, self.loc(agent.id, "decide", "rules", k, "when")
                )
        if isinstance(agent, LoopAgent):
            for j, tool in enumerate(agent.loop.tools):
                if tool.approval is not None and tool.approval != "required":
                    args = self.tool_args(tool)
                    resolver = (
                        self.resolver_with({"__Args__": args})
                        if isinstance(args, type)
                        else self.cat.resolve
                    )
                    env_args = {
                        "args": TypeRef("model", name="__Args__")
                        if isinstance(args, type)
                        else TypeRef("scalar", name="json")
                    }
                    self.check_condition(
                        tool.approval.when,
                        env_args,
                        self.loc(agent.id, "loop", "tools", j, "approval", "when"),
                        resolver,
                    )

    def templates_of(self, agent: Any) -> Iterator[tuple[tuple[str | int, ...], str]]:
        if isinstance(agent, ToolAgent):
            tool = agent.tool
            if tool.use == "db":
                yield from _strings(tool.where, ("tool", "where"))
                yield from _strings(tool.values, ("tool", "values"))
            elif tool.use == "http":
                yield ("tool", "url"), tool.url
                yield from _strings(tool.headers, ("tool", "headers"))
                yield from _strings(tool.query, ("tool", "query"))
                yield from _strings(tool.body, ("tool", "body"))
            elif tool.use == "storage":
                for attr in ("key", "to_key", "prefix", "cursor"):
                    text = getattr(tool, attr)
                    if text:
                        yield ("tool", attr), text
                yield from _strings(tool.body, ("tool", "body"))
                yield from _strings(tool.metadata, ("tool", "metadata"))
            else:
                yield from _strings(tool.args, ("tool", "args"))
        elif isinstance(agent, MapAgent):
            yield from _strings(agent.map.with_, ("map", "with"))
        elif isinstance(agent, ParallelAgent):
            for i, branch in enumerate(agent.parallel.branches):
                for loc, text in self.templates_of(branch):
                    yield ("parallel", "branches", i, *loc), text
        elif isinstance(agent, LlmAgent):
            for key in ("system", "prompt"):
                text = getattr(agent.llm, key)
                if text and not text.startswith("artifact://"):
                    yield ("llm", key), text
        elif isinstance(agent, HumanAgent):
            for key in ("title", "instruction", "assignee"):
                text = getattr(agent.human, key)
                if text:
                    yield ("human", key), text
        elif isinstance(agent, CodeAgent):
            yield from _strings(agent.code.with_, ("code", "with"))

    def tool_args(self, tool: Any) -> type[BaseModel] | None:
        try:
            if isinstance(tool, AgentToolRef) and tool.agent in self.by_id:
                return build_model(
                    "args", field_types(self.by_id[tool.agent].inputs), self.cat.resolve
                )
            if isinstance(tool, WorkflowToolRef) and tool.workflow in self.cat.workflows:
                child = self.cat.workflows[tool.workflow]
                return build_model("args", field_types(child.spec.trigger.input), self.cat.resolve)
            if isinstance(tool, CollectionToolRef):
                return build_model("args", field_types({"query": "str"}), self.cat.resolve)
        except (TELError, ValueError):
            return None
        return None

    def resolver_with(self, extra: dict[str, type[BaseModel]]) -> Any:
        def resolve(name: str, variant: str) -> type[BaseModel] | None:
            return extra.get(name) if name in extra else self.cat.resolve(name, variant)

        return resolve

    def check_template(self, text: str, env: dict[str, Any], loc: Loc, code: str) -> None:
        if "{{" not in text:
            return
        try:
            compile_template(text).check(env, self.cat.resolve)
        except (TELError, ValueError) as exc:
            self.out.error(code, loc, _tel_message(exc))

    def check_condition(
        self, text: str, env: dict[str, Any], loc: Loc, resolver: Any = None
    ) -> None:
        try:
            compile_expr(text).check(env, resolver or self.cat.resolve, expect="bool")
        except (TELError, ValueError) as exc:
            self.out.error("V008", loc, _tel_message(exc))

    # ── artifacts (V017) ─────────────────────────────────────────────────────

    def check_artifacts(self, agent: Any) -> None:
        for loc, site, ref in self.artifact_refs(agent):
            self.check_artifact(ref, site, self.loc(agent.id, *loc))

    def artifact_refs(self, agent: Any) -> Iterator[tuple[tuple[str | int, ...], str, str]]:
        return artifact_refs(agent)

    def check_workflow_artifacts(self) -> None:
        for k, ref in enumerate(self.spec.hooks):
            self.check_artifact(ref, "hooks", ("spec", "hooks", k))
        if self.spec.supervisor and self.spec.supervisor.judge:
            self.check_artifact(
                self.spec.supervisor.judge, "judge", ("spec", "supervisor", "judge")
            )

    def check_artifact(self, ref: str, site: str, loc: Loc) -> None:
        body = ref[len("artifact://") :]
        name, _, version = body.partition("@")
        entry = self.cat.artifacts.get(name)
        if entry is None:
            self.out.error("V017", loc, f"artifact '{name}' is not defined")
            return
        type_, versions = entry
        allowed = REF_SITE_COMPAT.get(site)
        if allowed is not None and type_ not in allowed:
            self.out.error(
                "V017",
                loc,
                f"'{name}' is a {type_} artifact; this field takes {' or '.join(sorted(allowed))}",
            )
        if not version:
            self.out.warn(
                "V017",
                loc,
                f"'{ref}' is not pinned — use artifact://{name}@{max(versions) if versions else 1}",
            )
        elif int(version) not in versions:
            self.out.error(
                "V017", loc, f"'{name}' has no version {version} — versions: {sorted(versions)}"
            )
