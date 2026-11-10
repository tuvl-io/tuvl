"""Per-engine rules."""

from __future__ import annotations

from typing import Any

from tuvl.contract.facts import derived_effect, enum_outputs, field_types
from tuvl.contract.schema import (
    CodeAgent,
    DecideAgent,
    HumanAgent,
    LlmAgent,
    LoopAgent,
    MapAgent,
    ParallelAgent,
    PendingAgent,
    ToolAgent,
)
from tuvl.contract.types import TypeRef, format_type
from tuvl_insight.validate.diagnostics import Loc
from tuvl_insight.validate.signals import assignable, is_reserved
from tuvl_insight.validate.workflow.base import _Base
from tuvl_insight.validate.workflow.helpers import _EFFECT_RANK, _waits_for_a_person


class _Engines(_Base):
    def check_memory(self, agent: LlmAgent | LoopAgent) -> None:
        """V043: the memory's owner cannot be chosen by an anonymous caller, and what is stored is the agent's own."""
        m = (agent.llm if isinstance(agent, LlmAgent) else agent.loop).memory
        if m is None:
            return
        base = (agent.engine, "memory")
        self.check_collection(m.collection, self.loc(agent.id, *base, "collection"))
        http = self.spec.trigger.http
        if m.key not in ("principal", "tenant") and http is not None and http.public:
            self.out.error(
                "V043",
                self.loc(agent.id, *base, "key"),
                f"'{agent.id}' names the memory owner from input on a public trigger: anyone could read "
                "or write another person's memory — use key: principal, or make the trigger private",
            )
        fields = {**field_types(agent.inputs), **field_types(agent.outputs)}
        for name in m.write or []:
            t = fields.get(name)
            if t is None:
                self.out.error(
                    "V043",
                    self.loc(agent.id, *base, "write"),
                    f"memory.write names '{name}', which is neither an input nor an output of '{agent.id}'",
                )
                continue
            ref = t.item if t.kind == "list" and t.item is not None else t
            if ref.kind == "model" and self.cat.secure_fields.get(ref.name or ""):
                self.out.error(
                    "V043",
                    self.loc(agent.id, *base, "write"),
                    f"memory.write '{name}' carries secure fields of {ref.name}: secure fields are never remembered",
                )

    def check_stream(self, agent: LlmAgent) -> None:
        """V039: ``stream_tokens`` streams one ``str`` output and never text a guardrail could refuse."""
        if not agent.llm.stream_tokens:
            return
        loc = self.loc(agent.id, "llm", "stream_tokens")
        if agent.guardrails.output:
            self.out.error(
                "V039",
                loc,
                f"'{agent.id}' streams its answer, which an output guardrail could still refuse "
                f"— drop guardrails.output or stream_tokens",
            )
        fields = field_types(agent.outputs)
        texts = [k for k, t in fields.items() if t.kind == "scalar" and t.name == "str"]
        wanted = agent.llm.stream_field
        if wanted is None:
            if len(texts) != 1:
                have = ", ".join(texts) if texts else "none"
                self.out.error(
                    "V039",
                    loc,
                    f"'{agent.id}' must say which output to stream: set llm.stream_field "
                    f"(str outputs: {have})",
                )
        elif wanted not in fields:
            self.out.error(
                "V039",
                self.loc(agent.id, "llm", "stream_field"),
                f"stream_field '{wanted}' is not an output of '{agent.id}'",
            )
        elif wanted not in texts:
            self.out.error(
                "V039",
                self.loc(agent.id, "llm", "stream_field"),
                f"stream_field '{wanted}' is {fields[wanted]}, not str",
            )

    def check_budget(self, agent: Any) -> None:
        b = agent.budget
        need: tuple[str, ...] = ()
        if isinstance(agent, LlmAgent):
            need = ("max_tokens", "timeout")
        elif isinstance(agent, LoopAgent):
            need = ("max_iterations", "max_tokens", "max_tool_calls", "timeout")
        elif isinstance(agent, DecideAgent) and agent.decide.model is not None:
            need = ("timeout",)
            if self.cat.agent_models.get(agent.decide.model.ref) == "llm":
                need += ("max_tokens",)
        for name in need:
            if getattr(b, name) is None:
                self.out.error(
                    "V011",
                    self.loc(agent.id, "budget"),
                    f"{agent.engine} agents must set budget.{name}",
                )
        loop_only = ("max_iterations", "max_tool_calls")
        if not isinstance(agent, LoopAgent):
            for name in loop_only:
                if getattr(b, name) is not None:
                    self.out.warn(
                        "V011",
                        self.loc(agent.id, "budget", name),
                        f"budget.{name} only applies to loop agents — ignored",
                    )

    def check_engine(self, agent: Any) -> None:
        if isinstance(agent, PendingAgent):
            self.out.warn(
                "V010",
                self.loc(agent.id, "engine"),
                f"'{agent.id}' is pending — contract only; 'tuvl ship' rejects it",
            )
        elif isinstance(agent, DecideAgent):
            self.check_decide(agent)
        elif isinstance(agent, HumanAgent):
            self.check_human(agent)
        elif isinstance(agent, LlmAgent):
            self.check_stream(agent)
            self.check_memory(agent)
        elif isinstance(agent, LoopAgent):
            self.check_memory(agent)
        elif isinstance(agent, MapAgent):
            self.check_map(agent)
        elif isinstance(agent, ParallelAgent):
            self.check_parallel(agent)
        elif isinstance(agent, ToolAgent) and agent.tool.use == "db":
            self.check_db_result(agent)
        elif isinstance(agent, ToolAgent) and agent.tool.use == "storage":
            self.check_storage(agent)
        elif isinstance(agent, CodeAgent) and agent.code.run.startswith("tuvl."):
            self.check_builtin(agent)
        elif (
            isinstance(agent, CodeAgent)
            and self.cat.code_agents is not None
            and agent.code.run not in self.cat.code_agents
        ):
            self.out.error(
                "V025",
                self.loc(agent.id, "code", "run"),
                f"no @agent('{agent.code.run}') in agents/ — write agents/{agent.code.run}.py",
            )
        self.check_effect(agent)

    def check_builtin(self, agent: CodeAgent) -> None:
        from tuvl.runtime.builtins import DATA_SEARCH, MEMORY_RECALL, PARAMS

        run = agent.code.run
        if run not in PARAMS:
            self.out.error(
                "V025", self.loc(agent.id, "code", "run"), f"'{run}' is not a tuvl built-in agent"
            )
            return
        required, optional = PARAMS[run]
        given = set(agent.code.with_)
        for key in sorted(required - given):
            self.out.error(
                "V025", self.loc(agent.id, "code", "with"), f"{run} needs code.with.{key}"
            )
        for key in sorted(given - required - optional):
            self.out.error(
                "V025", self.loc(agent.id, "code", "with", key), f"{run} does not take '{key}'"
            )
        self.check_collection(
            agent.code.with_.get("collection"), self.loc(agent.id, "code", "with", "collection")
        )
        outputs = field_types(agent.outputs)
        if run == DATA_SEARCH and (
            len(outputs) != 1 or next(iter(outputs.values())).kind != "list"
        ):
            self.out.error(
                "V007",
                self.loc(agent.id, "outputs"),
                f"{run} writes its results to exactly one list output, e.g. passages: list[json]",
            )
        if run == MEMORY_RECALL and (
            len(outputs) != 1 or next(iter(outputs.values())).kind != "list"
        ):
            self.out.error(
                "V007",
                self.loc(agent.id, "outputs"),
                f"{run} writes its results to exactly one list output, e.g. memories: list[json]",
            )
        if run in (DATA_SEARCH, MEMORY_RECALL) and agent.effect != "read":
            self.out.warn(
                "V015", self.loc(agent.id, "effect"), f"{run} only reads — declare effect: read"
            )

    def check_collection(self, name: Any, loc: Loc) -> None:
        """V025/V014 helper: a literal collection name must be one the project defines."""
        collections = self.cat.collections
        if collections is None or not isinstance(name, str) or "{{" in name:
            return
        if name not in collections:
            known = ", ".join(sorted(collections)) or "none are defined"
            self.out.error(
                "V014", loc, f"collection '{name}' is not defined — collections: {known}"
            )

    def check_effect(self, agent: Any) -> None:
        """V015: a write/destructive step with later steps that can fail needs a
        compensator; a declared effect must not be weaker than the derived one."""
        derived = derived_effect(agent)
        if derived is None:
            return
        declared = getattr(agent, "effect", None) if isinstance(agent, ToolAgent) else None
        # "POST is a write" is only a presumption — search and screening APIs
        # often read via POST — so a declared read wins for http body methods.
        presumed = (
            isinstance(agent, ToolAgent)
            and agent.tool.use == "http"
            and agent.tool.method in ("POST", "PUT", "PATCH")
        )
        if declared is not None and _EFFECT_RANK[declared] < _EFFECT_RANK[derived] and not presumed:
            self.out.warn(
                "V015",
                self.loc(agent.id, "effect"),
                f"declared effect '{declared}' is weaker than the derived '{derived}'",
            )
        effect = declared or derived
        if effect == "read" or agent.compensate:
            return
        continues = [
            target
            for signal, target in agent.routes.items()
            if not is_reserved(signal) and target != "END" and not target.startswith("END.")
        ]
        if continues:
            self.out.warn(
                "V015",
                self.loc(agent.id, "compensate"),
                f"'{agent.id}' has a {effect} effect and later steps ({', '.join(sorted(set(continues)))}) "
                f"can fail — declare 'compensate' so a failed run can undo it",
            )

    def check_storage(self, agent: ToolAgent) -> None:
        """V030: the store exists; V031: it allows the op; V032: literal keys are safe;
        V033: a presign lifetime within the store's limit; V007: the result's output type."""
        from tuvl.storage.keys import literal_key_problem

        tool = agent.tool
        assert tool.use == "storage"
        stores = self.cat.stores
        spec = stores.get(tool.store) if stores is not None else None
        if stores is not None and spec is None:
            known = ", ".join(sorted(stores)) or "none declared"
            self.out.error(
                "V030",
                self.loc(agent.id, "tool", "store"),
                f"no Storage '{tool.store}' ({known}) — declare it in a kind: Storage file",
            )
        writes = tool.op in ("put", "copy", "delete") or (
            tool.op == "presign" and tool.method == "PUT"
        )
        if spec is not None and spec.readonly and writes:
            self.out.error(
                "V031",
                self.loc(agent.id, "tool", "op"),
                f"store '{tool.store}' is read-only; op '{tool.op}' is refused",
            )
        if tool.op == "copy" and tool.to_store and stores is not None:
            target = stores.get(tool.to_store)
            if target is None:
                self.out.error(
                    "V030",
                    self.loc(agent.id, "tool", "to_store"),
                    f"no Storage '{tool.to_store}'",
                )
            elif target.readonly:
                self.out.error(
                    "V031",
                    self.loc(agent.id, "tool", "to_store"),
                    f"store '{tool.to_store}' is read-only; it cannot receive a copy",
                )
        for attr in ("key", "to_key"):
            text = getattr(tool, attr)
            problem = literal_key_problem(text) if text else None
            if problem:
                self.out.error("V032", self.loc(agent.id, "tool", attr), f"{attr}: {problem}")
        if (
            spec is not None
            and tool.op == "presign"
            and tool.expires_in is not None
            and tool.expires_in > spec.presign_max_seconds
        ):
            self.out.error(
                "V033",
                self.loc(agent.id, "tool", "expires_in"),
                f"expires_in {tool.expires_in}s exceeds the store's presign_max_seconds "
                f"({spec.presign_max_seconds})",
            )
        self.check_storage_result(agent)

    def check_storage_result(self, agent: ToolAgent) -> None:
        tool = agent.tool
        assert tool.use == "storage"
        outputs = field_types(agent.outputs)
        loc = self.loc(agent.id, "outputs")
        if tool.op == "delete":
            if outputs:
                self.out.error(
                    "V007", loc, "a storage 'delete' produces no output — remove 'outputs'"
                )
            return
        if tool.map or not outputs:
            return
        if len(outputs) > 1:
            self.out.error(
                "V007",
                loc,
                f"a storage '{tool.op}' writes its result to one output unless 'map' is set; "
                f"'{agent.id}' declares {len(outputs)}",
            )
            return
        key, t = next(iter(outputs.items()))
        want = {
            "put": "json",
            "head": "json",
            "copy": "json",
            "get": "json" if tool.as_ == "json" else "str",
            "list": "list[json]",
            "presign": "str",
        }[tool.op]
        ok = (
            (
                t.kind == "list"
                and t.item is not None
                and t.item.kind == "scalar"
                and t.item.name == "json"
            )
            if want == "list[json]"
            else (t.kind == "scalar" and t.name == want)
        )
        if not ok:
            self.out.error(
                "V007",
                self.loc(agent.id, "outputs", key),
                f"a storage '{tool.op}' returns {want}, not {format_type(t)}",
            )

    def check_parallel(self, agent: ParallelAgent) -> None:
        """V040: every branch only reads; V041: branch inputs come from this agent and its outputs are
        exactly the branches' outputs, with no name twice; V042: not under a per-run transaction."""
        p = agent.parallel
        if self.spec.transaction == "per_run":
            self.out.error(
                "V042",
                self.loc(agent.id, "engine"),
                "a parallel step cannot run under transaction: per_run (its branches use sessions of their "
                "own); use per_agent",
            )
        inputs = field_types(agent.inputs)
        produced: dict[str, tuple[str, TypeRef]] = {}
        for i, branch in enumerate(p.branches):
            where = (agent.id, "parallel", "branches", i)
            effect = (
                derived_effect(branch)
                if isinstance(branch, ToolAgent)
                else getattr(branch, "effect", "read")
            )
            if isinstance(branch, ToolAgent) and branch.tool.use == "mcp":
                effect = "write"  # an MCP tool's effect is unknown until it is listed
            if effect != "read":
                self.out.error(
                    "V040",
                    ("spec", "agents", *where, "engine"),
                    f"branch '{branch.id}' has effect '{effect}': branches of a parallel step only read "
                    "(an MCP tool cannot be declared read; run writes in their own steps)",
                )
            for name, t in field_types(branch.inputs).items():
                given = inputs.get(name)
                if given is None:
                    self.out.error(
                        "V041",
                        ("spec", "agents", *where, "inputs", name),
                        f"branch '{branch.id}' reads '{name}', which '{agent.id}' does not take: add it "
                        "to the step's inputs",
                    )
                else:
                    why = assignable(given, t)
                    if why:
                        self.out.error(
                            "V007",
                            ("spec", "agents", *where, "inputs", name),
                            f"branch '{branch.id}' input '{name}': {why}",
                        )
            for name, t in field_types(branch.outputs).items():
                if name in produced:
                    self.out.error(
                        "V041",
                        ("spec", "agents", *where, "outputs", name),
                        f"'{name}' is produced by both '{produced[name][0]}' and '{branch.id}'",
                    )
                produced[name] = (branch.id, t)
            self.check_engine(branch)
        declared = field_types(agent.outputs)
        for name in sorted(set(produced) - set(declared)):
            self.out.error(
                "V041",
                self.loc(agent.id, "outputs"),
                f"branch '{produced[name][0]}' produces '{name}' but '{agent.id}' does not declare it",
            )
        for name, t in declared.items():
            if name not in produced:
                self.out.error(
                    "V041",
                    self.loc(agent.id, "outputs", name),
                    f"'{name}' is not produced by any branch",
                )
            elif p.on_error == "collect" and not t.optional:
                self.out.error(
                    "V041",
                    self.loc(agent.id, "outputs", name),
                    f"with on_error: collect a failed branch leaves '{name}' null: declare it optional "
                    f"('{format_type(t)}?')",
                )
            else:
                why = assignable(produced[name][1], t)
                if why:
                    self.out.error(
                        "V007",
                        self.loc(agent.id, "outputs", name),
                        f"'{name}' from branch '{produced[name][0]}': {why}",
                    )

    def check_map(self, agent: MapAgent) -> None:
        """V035: the map is well-formed (list input, child workflow, child inputs); V036: the child
        finishes on its own; V007: item and result types; V015: a declared effect not weaker than
        the child's."""
        from tuvl.contract.surface import workflow_effect

        p = agent.map
        inputs = field_types(agent.inputs)
        over = inputs.get(p.over)
        if over is None:
            self.out.error(
                "V035",
                self.loc(agent.id, "map", "over"),
                f"'{p.over}' is not one of the agent's inputs — declare it in 'inputs'",
            )
        elif over.kind != "list" or over.item is None:
            self.out.error(
                "V035",
                self.loc(agent.id, "map", "over"),
                f"'{p.over}' is {format_type(over)}, not a list",
            )

        outputs = field_types(agent.outputs)
        if len(outputs) != 1:
            self.out.error(
                "V007",
                self.loc(agent.id, "outputs"),
                f"a map writes its results to one list output; '{agent.id}' declares {len(outputs)}",
            )
        else:
            key, t = next(iter(outputs.items()))
            if t.kind != "list":
                self.out.error(
                    "V007",
                    self.loc(agent.id, "outputs", key),
                    f"a map returns a list of results, not {format_type(t)} (try list[json])",
                )

        if p.workflow == self.doc.metadata.name:
            self.out.error(
                "V035",
                self.loc(agent.id, "map", "workflow"),
                "a workflow cannot map over itself",
            )
            return
        child = self.cat.workflows.get(p.workflow)
        if child is None:
            if self.cat.workflows:
                self.out.error(
                    "V035",
                    self.loc(agent.id, "map", "workflow"),
                    f"workflow '{p.workflow}' is not defined in this project",
                )
            return

        child_inputs = field_types(child.spec.trigger.input)
        if p.as_ not in child_inputs:
            self.out.error(
                "V035",
                self.loc(agent.id, "map", "as"),
                f"'{p.workflow}' has no trigger input '{p.as_}' to receive each item "
                f"(it takes: {', '.join(child_inputs) or 'nothing'})",
            )
        elif over is not None and over.kind == "list" and over.item is not None:
            why = assignable(over.item, child_inputs[p.as_])
            if why:
                self.out.error(
                    "V007",
                    self.loc(agent.id, "map", "as"),
                    f"an item of '{p.over}' cannot be the input '{p.as_}' of '{p.workflow}': {why}",
                )
        for name in p.with_:
            if name not in child_inputs:
                self.out.error(
                    "V035",
                    self.loc(agent.id, "map", "with", name),
                    f"'{p.workflow}' has no trigger input '{name}'",
                )
            elif name == p.as_:
                self.out.error(
                    "V035",
                    self.loc(agent.id, "map", "with", name),
                    f"'{name}' receives each item; remove it from 'with'",
                )
        missing = [
            n for n, t in child_inputs.items() if not t.optional and n != p.as_ and n not in p.with_
        ]
        if missing:
            self.out.error(
                "V035",
                self.loc(agent.id, "map", "with"),
                f"'{p.workflow}' needs {', '.join(missing)}: pass them in 'with'",
            )

        for waiting in child.spec.agents:
            why = _waits_for_a_person(waiting)
            if why:
                self.out.error(
                    "V036",
                    self.loc(agent.id, "map", "workflow"),
                    f"'{p.workflow}' step '{waiting.id}' {why}; a mapped workflow must finish on "
                    "its own",
                )
        derived = workflow_effect(child)
        if _EFFECT_RANK[agent.effect] < _EFFECT_RANK[derived]:
            self.out.warn(
                "V015",
                self.loc(agent.id, "effect"),
                f"declared effect '{agent.effect}' is weaker than '{p.workflow}' ({derived})",
            )

    def check_db_result(self, agent: ToolAgent) -> None:
        """A db tool's result lands in its single output, or is discarded when
        it declares none (spec Q11)."""
        tool = agent.tool
        assert tool.use == "db"
        outputs = field_types(agent.outputs)
        loc = self.loc(agent.id, "outputs")
        if tool.op == "delete":
            if outputs:
                self.out.error("V007", loc, "a db 'delete' produces no output — remove 'outputs'")
            return
        if not outputs:
            return
        if len(outputs) > 1:
            self.out.error(
                "V007",
                loc,
                f"a db '{tool.op}' writes its result to one output; '{agent.id}' declares {len(outputs)}",
            )
            return
        key, t = next(iter(outputs.items()))
        want_list = tool.op == "list"
        row = t.item if want_list and t.kind == "list" else t
        if (
            (want_list and t.kind != "list")
            or row is None
            or row.kind != "model"
            or row.name != tool.model
        ):
            expected = f"list[{tool.model}]" if want_list else tool.model
            self.out.error(
                "V007",
                self.loc(agent.id, "outputs", key),
                f"a db '{tool.op}' on {tool.model} returns {expected}, not {format_type(t)}",
            )

    def check_decide(self, agent: DecideAgent) -> None:
        if agent.signal_outputs is not None:
            self.out.error(
                "V012",
                self.loc(agent.id, "outputs"),
                "decide agents have one decision output — per-signal outputs do not apply",
            )
            return
        enums = enum_outputs(agent)
        outputs = field_types(agent.outputs)
        if len(outputs) != 1 or len(enums) != 1:
            self.out.error(
                "V012",
                self.loc(agent.id, "outputs"),
                "a decide agent has exactly one output, of a non-optional enum type",
            )
            return
        values = set(next(iter(enums.values())).values)
        outcomes = {s for s in agent.routes if not is_reserved(s)}
        for missing in sorted(values - outcomes):
            self.out.error(
                "V012", self.loc(agent.id, "routes"), f"decision '{missing}' is not routed"
            )
        for extra in sorted(outcomes - values):
            self.out.error(
                "V012",
                self.loc(agent.id, "routes", extra),
                f"'{extra}' is not a value of the decision enum",
            )
        if isinstance(agent.decide.rules, list):
            for k, rule in enumerate(agent.decide.rules):
                if rule.then not in values:
                    self.out.error(
                        "V012",
                        self.loc(agent.id, "decide", "rules", k, "then"),
                        f"'{rule.then}' is not a value of the decision enum",
                    )
        model = agent.decide.model
        if (model is None or agent.decide.shadow) and "low_confidence" in agent.routes:
            why = "shadow mode" if model is not None else "no model is configured"
            self.out.error(
                "V012",
                self.loc(agent.id, "routes", "low_confidence"),
                f"'low_confidence' is never emitted — {why}",
            )
        if model is not None:
            self.check_decision_provider(agent)
        if model is not None and not agent.decide.shadow and not agent.decide.rules:
            self.check_model_only(agent)

    def check_decision_provider(self, agent: DecideAgent) -> None:
        """V026: the provider is installed; a hosted model is pinned."""
        assert agent.decide.model is not None
        info = self.cat.decision_models.get(agent.decide.model.ref)
        if info is None:
            return  # an llm AgentModel decides through the built-in model client
        provider, model = info
        loc = self.loc(agent.id, "decide", "model", "ref")
        from tuvl.runtime.decision import PROVIDERS, load_entry_point_providers

        if provider not in PROVIDERS:
            load_entry_point_providers()
        if provider not in PROVIDERS:
            self.out.error(
                "V026",
                loc,
                f"decision provider '{provider}' is not installed — register it through the "
                "tuvl.decision_providers entry point",
            )
        if provider == "laya":
            return  # model names a checkpoint; the laya-serve image pins the weights
        if model.endswith("latest") or not any(ch.isdigit() for ch in model.split("/")[-1]):
            self.out.warn(
                "V026",
                loc,
                f"decision model '{model}' is not pinned to a version — pin it for reproducible decisions",
            )

    def check_model_only(self, agent: DecideAgent) -> None:
        """V013: a decision only a model makes reaches a write/destructive step
        with no human before it and no compensator on it (``allow``: accepted on purpose)."""
        if self.spec.policy.model_only_decisions == "allow":
            return
        seen: set[str] = set()
        frontier = [t for s, t in agent.routes.items() if not is_reserved(s)]
        risky: list[str] = []
        while frontier:
            target = frontier.pop()
            if target in seen or target == "END" or target.startswith("END."):
                continue
            seen.add(target)
            nxt = self.by_id.get(target)
            if nxt is None or isinstance(nxt, HumanAgent):
                continue  # a person sees it before anything is written
            effect = derived_effect(nxt) or (
                str(nxt.effect) if isinstance(nxt, CodeAgent) else None
            )
            if effect in ("write", "destructive") and not nxt.compensate:
                risky.append(target)
            frontier += [t for s, t in nxt.routes.items() if not is_reserved(s)]
        if not risky:
            return
        message = (
            f"'{agent.id}' decides with a model and no rules, and feeds "
            f"{', '.join(sorted(risky))} (write) with no human or compensate in between"
        )
        loc = self.loc(agent.id, "decide", "model")
        if self.spec.policy.model_only_decisions == "error":
            self.out.error("V013", loc, message)
        else:
            self.out.warn("V013", loc, message)

    def check_human(self, agent: HumanAgent) -> None:
        outputs = field_types(agent.outputs)
        route_on = agent.human.route_on
        if route_on is not None:
            t = outputs.get(route_on)
            if t is None:
                self.out.error(
                    "V016",
                    self.loc(agent.id, "human", "route_on"),
                    f"route_on '{route_on}' is not an output",
                )
            elif t.optional or not (t.kind == "enum" or (t.kind == "scalar" and t.name == "bool")):
                self.out.error(
                    "V016",
                    self.loc(agent.id, "human", "route_on"),
                    f"route_on must name a non-optional bool or enum output, got {t}",
                )
        for name in agent.human.form:
            if name not in outputs:
                self.out.error(
                    "V016",
                    self.loc(agent.id, "human", "form", name),
                    f"form field '{name}' is not an output",
                )
        inputs = field_types(agent.inputs)
        for k, name in enumerate(agent.human.show):
            if name not in inputs:
                self.out.error(
                    "V008",
                    self.loc(agent.id, "human", "show", k),
                    f"'{name}' is not a declared input",
                )
