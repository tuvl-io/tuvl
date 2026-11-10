"""Standalone helpers shared by the checks and by the runtime."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from tuvl.contract.schema import (
    HumanAgent,
    LoopAgent,
    PendingAgent,
)
from tuvl.contract.tel import TELError

_TRIGGER = "$trigger"


# ── helpers ──────────────────────────────────────────────────────────────────


def _merge(a: dict[str, frozenset[str]], b: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, frozenset()) | v
    return out


_EFFECT_RANK = {"read": 0, "write": 1, "destructive": 2}


def _waits_for_a_person(agent: Any) -> str | None:
    """Why *agent* would leave its run waiting (so a mapped workflow cannot contain it)."""
    if isinstance(agent, HumanAgent):
        return "waits for a person (human)"
    if isinstance(agent, PendingAgent):
        return "is pending (contract only)"
    if isinstance(agent, LoopAgent) and any(
        getattr(tool, "approval", None) is not None for tool in agent.loop.tools
    ):
        return "has a loop tool that needs approval"
    return None


def _strings(value: Any, loc: tuple[str | int, ...]) -> Iterator[tuple[tuple[str | int, ...], str]]:
    if isinstance(value, str):
        yield loc, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, (*loc, str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, (*loc, i))


def _tel_message(exc: Exception) -> str:
    if isinstance(exc, TELError):
        if exc.text and exc.pos is not None:
            return f"{exc.message} (column {exc.pos + 1} of {exc.text!r})"
        return exc.message
    return str(exc)


def _sccs(edges: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components (iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in edges:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            succ = edges.get(node, [])
            if i < len(succ):
                work.append((node, i + 1))
                nxt = succ[i]
                if nxt not in edges:
                    continue
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                out.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return out
