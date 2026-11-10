"""Identity, entry, routes and cycles of the agent graph (V002-V005)."""

from __future__ import annotations

from typing import Any

from tuvl.contract.schema import (
    AgentToolRef,
    CodeAgent,
    CollectionToolRef,
    DecideAgent,
    HumanAgent,
    LlmAgent,
    LoopAgent,
    PendingAgent,
    ToolAgent,
    WorkflowToolRef,
)
from tuvl_insight.validate.signals import is_reserved, signals_of
from tuvl_insight.validate.workflow.base import _Base
from tuvl_insight.validate.workflow.helpers import _sccs


class _Topology(_Base):
    def check_ids(self) -> None:
        for i, agent in enumerate(self.spec.agents):
            if agent.id in self.by_id:
                self.out.error(
                    "V002", ("spec", "agents", i, "id"), f"duplicate agent id '{agent.id}'"
                )
                continue
            self.by_id[agent.id] = agent
            self.pos[agent.id] = i

    def collect_off_spine(self) -> None:
        for agent in self.by_id.values():
            if isinstance(agent, LoopAgent):
                for j, tool in enumerate(agent.loop.tools):
                    if isinstance(tool, AgentToolRef):
                        if tool.agent not in self.by_id:
                            self.out.error(
                                "V014",
                                self.loc(agent.id, "loop", "tools", j, "agent"),
                                f"tool agent '{tool.agent}' is not defined in this workflow",
                            )
                        elif tool.agent == agent.id:
                            self.out.error(
                                "V014",
                                self.loc(agent.id, "loop", "tools", j, "agent"),
                                "a loop cannot call itself as a tool",
                            )
                        elif isinstance(self.by_id[tool.agent], (HumanAgent, LoopAgent)):
                            self.out.error(
                                "V014",
                                self.loc(agent.id, "loop", "tools", j, "agent"),
                                f"'{tool.agent}' is a {self.by_id[tool.agent].engine} agent — "
                                f"a loop tool must finish on its own (use approval for people)",
                            )
                        else:
                            self.off_spine.add(tool.agent)
                    elif isinstance(tool, CollectionToolRef):
                        self.check_collection(
                            tool.collection, self.loc(agent.id, "loop", "tools", j, "collection")
                        )
                    elif (
                        isinstance(tool, WorkflowToolRef)
                        and self.cat.workflows
                        and tool.workflow not in self.cat.workflows
                    ):
                        self.out.error(
                            "V014",
                            self.loc(agent.id, "loop", "tools", j, "workflow"),
                            f"workflow '{tool.workflow}' is not defined in this project",
                        )
            if agent.compensate:
                if agent.compensate not in self.by_id:
                    self.out.error(
                        "V003",
                        self.loc(agent.id, "compensate"),
                        f"compensator '{agent.compensate}' is not defined",
                    )
                elif agent.compensate == agent.id:
                    self.out.error(
                        "V003",
                        self.loc(agent.id, "compensate"),
                        "an agent cannot compensate itself",
                    )
                else:
                    self.off_spine.add(agent.compensate)

    def check_entry(self) -> None:
        start = self.spec.start
        if start is not None and start not in self.by_id:
            self.out.error("V003", ("spec", "start"), f"start agent '{start}' is not defined")
        elif self.spec.entry in self.off_spine:
            self.out.error(
                "V003",
                ("spec", "start") if start else ("spec", "agents", 0),
                f"the entry agent '{self.spec.entry}' is off-spine (a loop tool or compensator) — set 'start'",
            )

    def check_routes(self, agent: Any) -> None:
        for signal, target in agent.routes.items():
            loc = self.loc(agent.id, "routes", signal)
            if target == "END" or target.startswith("END."):
                end = target[4:] if target.startswith("END.") else "default"
                if end not in self.spec.outputs:
                    declared = ", ".join(sorted(self.spec.outputs)) or "none"
                    self.out.error(
                        "V003", loc, f"'{target}' is not a declared output — outputs: {declared}"
                    )
            elif target not in self.by_id:
                self.out.error(
                    "V003", loc, f"route target '{target}' is not an agent, END, or END.<output>"
                )
            elif target in self.off_spine:
                self.out.error(
                    "V003",
                    loc,
                    f"'{target}' is off-spine (a loop tool or compensator) and cannot be a route target",
                )
            elif target == agent.id and isinstance(
                agent, (CodeAgent, ToolAgent, DecideAgent, PendingAgent)
            ):
                self.out.warn(
                    "V005", loc, f"'{agent.id}' routes to itself — an unbounded retry loop"
                )

        if not self.is_spine(agent.id):
            return
        sigs = signals_of(agent)
        if isinstance(agent, LoopAgent) and not sigs.normal:
            self.out.error(
                "V004",
                self.loc(agent.id, "routes"),
                "a loop needs at least one outcome route (a non-reserved signal)",
            )
        must = set(sigs.required) | (set() if isinstance(agent, DecideAgent) else set(sigs.normal))
        for signal in sorted(must - set(agent.routes)):
            self.out.error(
                "V004",
                self.loc(agent.id, "routes"),
                f"'{agent.id}' can emit '{signal}' but it is not routed",
            )
        if "error" not in agent.routes and not isinstance(agent, PendingAgent):
            self.out.warn(
                "V004",
                self.loc(agent.id, "routes"),
                "'error' is not routed — an error ends the run as failed",
            )
        for signal in agent.routes:
            if isinstance(agent, DecideAgent) and not is_reserved(signal):
                continue  # V012 compares decide outcomes with the enum
            if not sigs.can_emit(signal):
                self.out.error(
                    "V004",
                    self.loc(agent.id, "routes", signal),
                    f"'{agent.id}' never emits '{signal}'",
                )

    def spine_edges(self) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = {}
        for agent_id in self.flow.available:
            edges[agent_id] = [t for t in self.by_id[agent_id].routes.values() if self.is_spine(t)]
        return edges

    def check_cycles(self) -> None:
        # A cycle is safe only if every way around it passes a human or a
        # budgeted (llm/loop) agent: drop those and look for any cycle left.
        guards = (HumanAgent, LlmAgent, LoopAgent)
        edges = {
            n: [t for t in succ if not isinstance(self.by_id[t], guards)]
            for n, succ in self.spine_edges().items()
            if not isinstance(self.by_id[n], guards)
        }
        for scc in _sccs(edges):
            if len(scc) < 2:
                continue  # self-routes are reported in check_routes
            ordered = sorted(scc, key=lambda n: self.pos[n])
            self.out.warn(
                "V005",
                self.loc(ordered[0], "routes"),
                f"cycle {', '.join(ordered)} can repeat without passing a human or bounded agent",
            )
