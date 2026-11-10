"""Profile and cost (spec section 13)."""

from __future__ import annotations

from typing import Any

from tuvl.contract.facts import determinism_of
from tuvl.contract.schema import (
    DecideAgent,
    LlmAgent,
    LoopAgent,
    MapAgent,
    ParallelAgent,
    WorkflowToolRef,
)
from tuvl_insight.validate.workflow.base import _Base
from tuvl_insight.validate.workflow.helpers import _sccs
from tuvl_insight.validate.workflow.report import Profile


class _Profiling(_Base):
    def child_worst(self, name: str, depth: int) -> int:
        """Worst-case tokens of one run of workflow *name*; 0 when unknown or this workflow."""
        child = self.cat.workflows.get(name)
        if child is None or child.metadata.name == self.doc.metadata.name:
            return 0
        from tuvl_insight.validate.workflow import _Validator

        child_v = _Validator(child, self.cat)
        child_v.check_ids()
        child_v.collect_off_spine()
        child_v.analyse_flow()
        return child_v.worst_case(depth + 1) or 0

    def agent_tokens(self, agent: Any, depth: int = 0) -> int:
        tokens = 0
        if isinstance(agent, (LlmAgent, LoopAgent)) or (
            isinstance(agent, DecideAgent) and agent.decide.model is not None
        ):
            tokens = agent.budget.max_tokens or 0
        if isinstance(agent, LlmAgent) and agent.llm.memory is not None:
            tokens += agent.llm.memory.max_tokens
        if isinstance(agent, LoopAgent) and agent.loop.memory is not None:
            tokens += agent.loop.memory.max_tokens
        if isinstance(agent, LoopAgent) and depth < self.spec.policy.max_depth:
            for tool in agent.loop.tools:
                if isinstance(tool, WorkflowToolRef):
                    tokens += self.child_worst(tool.workflow, depth)
        if isinstance(agent, MapAgent) and depth < self.spec.policy.max_depth:
            tokens += self.child_worst(agent.map.workflow, depth) * agent.map.max_items
        if isinstance(agent, ParallelAgent):
            tokens += sum(self.agent_tokens(b, depth) for b in agent.parallel.branches)
        return tokens

    def determinism(self, agent: Any) -> str:
        """The agent's class; a map takes the strongest class of the workflow it runs."""
        if not isinstance(agent, MapAgent):
            return determinism_of(agent)
        child = self.cat.workflows.get(agent.map.workflow)
        if child is None or child.metadata.name == self.doc.metadata.name:
            return "deterministic"
        order = ("deterministic", "bounded", "external", "pending")
        return max(
            (determinism_of(a) for a in child.spec.agents), key=order.index, default=order[0]
        )

    def worst_case(self, depth: int = 0) -> int | None:
        edges = self.spine_edges()
        if not edges:
            return 0
        sccs = _sccs(edges)
        scc_of = {n: i for i, scc in enumerate(sccs) for n in scc}
        weight = [sum(self.agent_tokens(self.by_id[n], depth) for n in scc) for scc in sccs]
        unbounded = {
            i
            for i, scc in enumerate(sccs)
            if weight[i] > 0 and (len(scc) > 1 or any(n in edges.get(n, []) for n in scc))
        }
        memo: dict[int, int | None] = {}

        def best(i: int) -> int | None:
            if i in memo:
                return memo[i]
            if i in unbounded:
                memo[i] = None
                return None
            nxt: list[int | None] = []
            for n in sccs[i]:
                for t in edges.get(n, []):
                    j = scc_of[t]
                    if j != i:
                        nxt.append(best(j))
            if any(x is None for x in nxt):
                memo[i] = None
            else:
                memo[i] = weight[i] + max((x for x in nxt if x is not None), default=0)
            return memo[i]

        return best(scc_of[self.spec.entry]) if self.spec.entry in scc_of else 0

    def profile(self) -> Profile:
        counts = {"deterministic": 0, "bounded": 0, "external": 0, "pending": 0}
        judges = bool(self.spec.supervisor and self.spec.supervisor.judge)
        for agent in self.by_id.values():
            counts[self.determinism(agent)] += 1
            guardrails = getattr(agent, "guardrails", None)
            if guardrails is not None and any(
                self.cat.artifacts.get(r[len("artifact://") :].partition("@")[0], ("",))[0]
                == "judge"
                for gate in (guardrails.input, guardrails.output, guardrails.tools)
                for r in gate
            ):
                judges = True
            if (
                isinstance(agent, LoopAgent)
                and agent.loop.supervisor
                and agent.loop.supervisor.judge
            ):
                judges = True
        return Profile(
            agents=len(self.by_id),
            off_spine=len(self.off_spine & set(self.by_id)),
            worst_case_tokens=self.worst_case(),
            has_judges=judges,
            **counts,
        )
