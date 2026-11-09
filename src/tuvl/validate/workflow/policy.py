"""Models, trigger, types and policy (V018, V019)."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from tuvl.contract.facts import derived_effect, field_types
from tuvl.contract.schema import (
    AgentToolRef,
    CodeAgent,
    CollectionToolRef,
    DecideAgent,
    HumanAgent,
    LlmAgent,
    LoopAgent,
    ToolAgent,
    WorkflowToolRef,
)
from tuvl.contract.tel import TELError
from tuvl.contract.types import TypeRef, build_model
from tuvl.validate.diagnostics import Loc
from tuvl.validate.workflow.base import _Base
from tuvl.validate.workflow.helpers import _EFFECT_RANK
from tuvl.validate.workflow.report import Profile


class _Policy(_Base):
    def check_policy(self, profile: Profile) -> None:
        """V020: the workflow's ``policy`` checked statically (spec §13.3)."""
        policy = self.spec.policy
        loc = ("spec", "policy")
        if policy.max_tokens_per_run is not None:
            if profile.worst_case_tokens is None:
                self.out.error(
                    "V020",
                    (*loc, "max_tokens_per_run"),
                    "worst-case tokens are unbounded (a token-spending cycle) — "
                    "bound the cycle or drop max_tokens_per_run",
                )
            elif profile.worst_case_tokens > policy.max_tokens_per_run:
                self.out.error(
                    "V020",
                    (*loc, "max_tokens_per_run"),
                    f"worst-case {profile.worst_case_tokens} tokens exceeds "
                    f"max_tokens_per_run {policy.max_tokens_per_run}",
                )
        if policy.max_bounded_agents is not None and profile.bounded > policy.max_bounded_agents:
            self.out.error(
                "V020",
                (*loc, "max_bounded_agents"),
                f"{profile.bounded} bounded (model-driven) agents exceed max_bounded_agents "
                f"{policy.max_bounded_agents}",
            )
        if policy.require_human_before:
            self.check_human_before(set(policy.require_human_before))
        if not policy.allow_secure_to_llm:
            self.check_secure_to_llm()

    def check_human_before(self, guarded: set[str]) -> None:
        """Every path to a guarded effect passes a human (or, in a loop, an approval)."""
        seen: set[str] = set()
        frontier = [self.spec.entry]
        while frontier:
            current = frontier.pop()
            if current in seen or current == "END" or current.startswith("END."):
                continue
            seen.add(current)
            agent = self.by_id.get(current)
            if agent is None or isinstance(agent, HumanAgent):
                continue  # a person sees everything after this point
            effect = derived_effect(agent) or (
                str(agent.effect) if isinstance(agent, CodeAgent) else None
            )
            if effect in guarded:
                self.out.error(
                    "V020",
                    self.loc(agent.id, "engine"),
                    f"'{agent.id}' has a {effect} effect reachable without a human — "
                    f"policy.require_human_before",
                )
            if isinstance(agent, LoopAgent):
                for j, tool in enumerate(agent.loop.tools):
                    tool_effect = self.loop_tool_effect(tool)
                    if tool_effect in guarded and tool.approval is None:
                        self.out.error(
                            "V020",
                            self.loc(agent.id, "loop", "tools", j),
                            f"a {tool_effect} loop tool needs 'approval' — policy.require_human_before",
                        )
            frontier += list(agent.routes.values())

    def loop_tool_effect(self, tool: Any) -> str:
        if isinstance(tool, AgentToolRef) and tool.agent in self.by_id:
            target = self.by_id[tool.agent]
            return derived_effect(target) or str(getattr(target, "effect", None) or "read")
        if isinstance(tool, WorkflowToolRef) and tool.workflow in self.cat.workflows:
            child = self.cat.workflows[tool.workflow]
            effects = [
                derived_effect(a) or str(getattr(a, "effect", None) or "read")
                for a in child.spec.agents
            ]
            return max(effects, key=lambda e: _EFFECT_RANK.get(e, 0), default="read")
        if isinstance(tool, CollectionToolRef):
            return "read"
        return "write"  # an MCP tool's hints are only known at run time

    def check_secure_to_llm(self) -> None:
        """Secure model fields never reach a model unless policy allows (spec §16.4)."""
        for agent in self.by_id.values():
            reaches_model = isinstance(agent, (LlmAgent, LoopAgent)) or (
                isinstance(agent, DecideAgent) and agent.decide.model is not None
            )
            if not reaches_model:
                continue
            for key, t in field_types(agent.inputs).items():
                ref = t.item if t.kind == "list" and t.item is not None else t
                if ref.kind != "model":
                    continue
                secure = self.cat.secure_fields.get(ref.name or "")
                if secure:
                    self.out.error(
                        "V020",
                        self.loc(agent.id, "inputs", key),
                        f"'{key}' carries secure fields {sorted(secure)} of {ref.name} into a model — "
                        f"pass only the fields it needs, or set policy.allow_secure_to_llm",
                    )

    def check_transaction(self) -> None:
        """V021: a per_run transaction cannot wait — no human agents, no
        approval-gated loop tools, no scheduled (async) trigger."""
        if self.spec.transaction != "per_run":
            return
        for agent in self.by_id.values():
            waits = isinstance(agent, HumanAgent) or (
                isinstance(agent, LoopAgent)
                and any(tool.approval is not None for tool in agent.loop.tools)
            )
            if waits:
                self.out.error(
                    "V021",
                    self.loc(agent.id, "engine"),
                    f"'{agent.id}' can wait for a person — transaction: per_run cannot "
                    f"span that; use per_agent",
                )
        if self.spec.trigger.active_schedule:
            self.out.error(
                "V021",
                ("spec", "trigger", "schedule"),
                "transaction: per_run is synchronous only — a scheduled run is asynchronous",
            )

    def check_models(self) -> None:
        allowed = {pin.partition("@")[0] for pin in self.spec.models}
        for k, pin in enumerate(self.spec.models):
            name = pin.partition("@")[0]
            if name not in self.cat.models:
                self.out.error(
                    "V018", ("spec", "models", k), f"model '{name}' is not defined in this project"
                )
        for agent in self.by_id.values():
            if (
                isinstance(agent, ToolAgent)
                and agent.tool.use == "db"
                and agent.tool.model not in allowed
            ):
                self.out.error(
                    "V018",
                    self.loc(agent.id, "tool", "model"),
                    f"'{agent.tool.model}' is not in spec.models — add it to the allow-list",
                )

    def check_trigger(self) -> None:
        meta = self.doc.metadata
        http = self.spec.trigger.http
        if http is not None and http.public and (meta.required_scope or meta.required_group):
            self.out.error(
                "V019",
                ("spec", "trigger", "http", "public"),
                "public: true cannot be combined with required_scope / required_group",
            )
        mcp = self.spec.trigger.mcp
        if mcp is not None and mcp.enabled and not (meta.description or "").strip():
            self.out.error(
                "V019",
                ("spec", "trigger", "mcp"),
                "MCP export needs metadata.description — it becomes the tool description",
            )
        for key, t in self.trigger_types.items():
            self.check_type_models(t, ("spec", "trigger", "input", key))
        self.check_schedule_input()
        self.check_uploads()
        self.check_webhooks()

    def check_webhooks(self) -> None:
        ids = {a.id for a in self.spec.agents}
        for i, hook in enumerate(self.spec.webhooks):
            loc = ("spec", "webhooks", i)
            for stage in hook.stages:
                if stage not in ids:
                    self.out.error(
                        "V038", (*loc, "stages"), f"webhook '{hook.name}': no agent '{stage}'"
                    )
            if hook.allow_private:
                self.out.warn(
                    "V038",
                    (*loc, "allow_private"),
                    f"webhook '{hook.name}' may reach internal addresses and plain http",
                )

    def check_uploads(self) -> None:
        def has_file(t: TypeRef) -> bool:
            return (t.kind == "scalar" and t.name == "file") or (
                t.kind == "list" and t.item is not None and has_file(t.item)
            )

        def plain(t: TypeRef) -> bool:
            return (t.kind == "scalar" and t.name == "file") or (
                t.kind == "list"
                and t.item is not None
                and t.item.kind == "scalar"
                and t.item.name == "file"
            )

        files = []
        for key, t in self.trigger_types.items():
            if not has_file(t):
                continue
            if plain(t):
                files.append(key)
            else:
                self.out.error(
                    "V037",
                    ("spec", "trigger", "input", key),
                    "a file input is `file` or `list[file]`, not nested deeper",
                )
        trigger = self.spec.trigger
        http = trigger.active_http
        upload = http.upload if http else None
        if trigger.mcp is not None and trigger.mcp.enabled and files:
            self.out.error(
                "V037",
                ("spec", "trigger", "mcp"),
                f"MCP export cannot take file inputs ({', '.join(files)})",
            )
        if http is not None and files and upload is None:
            self.out.warn(
                "V037",
                ("spec", "trigger", "http"),
                f"the route has no upload, so it cannot receive {', '.join(files)} — "
                "add http.upload, or start this workflow only from another workflow or a schedule",
            )
        if upload is None:
            return
        loc = ("spec", "trigger", "http", "upload")
        if not files:
            self.out.error("V037", loc, "upload is declared but no trigger input is a file")
        if http is not None and http.public:
            self.out.warn("V037", loc, "a public route lets anyone write files to the store")
        stores = self.cat.stores
        if stores is None:
            return
        spec = stores.get(upload.store)
        if spec is None:
            known = ", ".join(sorted(stores)) or "none declared"
            self.out.error("V037", (*loc, "store"), f"no Storage '{upload.store}' ({known})")
        elif spec.readonly:
            self.out.error("V037", (*loc, "store"), f"store '{upload.store}' is read-only")
        elif upload.max_bytes > spec.max_object_bytes:
            self.out.error(
                "V037",
                (*loc, "max_bytes"),
                f"max_bytes {upload.max_bytes} exceeds the store's max_object_bytes "
                f"({spec.max_object_bytes})",
            )

    def check_schedule_input(self) -> None:
        schedule = self.spec.trigger.active_schedule
        if schedule is None:
            return
        try:
            model = build_model("schedule.input", self.trigger_types, self.cat.resolve)
        except (TELError, ValueError):
            return
        try:
            model.model_validate(schedule.input)
        except ValidationError as exc:
            for err in exc.errors(include_url=False):
                self.out.error(
                    "V019",
                    ("spec", "trigger", "schedule", "input", *err["loc"]),
                    f"scheduled input does not match trigger.input: {err['msg']}",
                )

    def check_types_resolve(self, agent: Any) -> None:
        for section in ("inputs", "outputs"):
            for key, t in field_types(getattr(agent, section)).items():
                self.check_type_models(t, self.loc(agent.id, section, key))

    def check_type_models(self, t: TypeRef, loc: Loc) -> None:
        for name, variant in sorted(t.model_refs()):
            if self.cat.resolve(name, variant) is None:
                what = "model" if name not in self.cat.models else f"variant '{variant}' of model"
                self.out.error("V018", loc, f"unknown {what} '{name}'")
