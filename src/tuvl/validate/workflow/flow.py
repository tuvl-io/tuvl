"""Data flow: what is available where (V005-V007, V009, V022)."""

from __future__ import annotations

from typing import Any

from tuvl.contract.facts import field_types
from tuvl.contract.tel import TELError, compile_template
from tuvl.contract.types import TypeRef, format_type
from tuvl.validate.signals import assignable, signals_of
from tuvl.validate.workflow.base import _Base
from tuvl.validate.workflow.helpers import (
    _TRIGGER,
    _merge,
    _strings,
    _tel_message,
)


class _DataFlow(_Base):
    def analyse_flow(self) -> None:
        entry = self.spec.entry
        if not self.is_spine(entry):
            return
        f = self.flow
        f.available[entry] = frozenset(self.trigger_types)
        f.producers[entry] = {k: frozenset({_TRIGGER}) for k in self.trigger_types}
        work = [entry]
        while work:
            n = work.pop()
            agent = self.by_id[n]
            normal = signals_of(agent).normal
            for signal, target in agent.routes.items():
                produces = signal in normal
                outs = (
                    frozenset(field_types(agent.outputs_for(signal))) if produces else frozenset()
                )
                avail = f.available[n] | outs if produces else f.available[n]
                prods = dict(f.producers[n])
                if produces:
                    for k in outs:
                        prods[k] = frozenset({n})
                if target == "END" or target.startswith("END."):
                    end = target[4:] if target.startswith("END.") else "default"
                    f.end_available[end] = avail & f.end_available.get(end, avail)
                    f.end_producers[end] = _merge(f.end_producers.get(end, {}), prods)
                    continue
                if not self.is_spine(target):
                    continue
                f.preds.setdefault(target, [])
                if (n, signal) not in f.preds[target]:
                    f.preds[target].append((n, signal))
                old_a, old_p = f.available.get(target), f.producers.get(target)
                new_a = avail if old_a is None else old_a & avail
                new_p = _merge(old_p or {}, prods)
                if new_a != old_a or new_p != old_p:
                    f.available[target] = new_a
                    f.producers[target] = new_p
                    work.append(target)

    def output_type(self, producer: str, key: str) -> TypeRef | None:
        """The exact type *producer* writes to *key* (not the optional union)."""
        if producer == _TRIGGER:
            return self.trigger_types.get(key)
        spec = self.by_id[producer].output_type(key)
        return field_types({key: spec}).get(key) if spec is not None else None

    def check_flow(self) -> None:
        f = self.flow
        for agent_id, agent in self.by_id.items():
            if not self.is_spine(agent_id):
                continue
            if agent_id not in f.available:
                if self.spec.entry in f.available:
                    self.out.warn(
                        "V005",
                        self.loc(agent_id, "id"),
                        f"'{agent_id}' is unreachable from the entry agent '{self.spec.entry}'",
                    )
                continue
            avail, prods = f.available[agent_id], f.producers[agent_id]
            for key, t in field_types(agent.inputs).items():
                loc = self.loc(agent_id, "inputs", key)
                if not t.optional and key not in avail:
                    self.out.error(
                        "V006",
                        loc,
                        f"input '{key}' is not produced on every path to '{agent_id}'{self.witness(agent_id, key)}",
                    )
                for p in sorted(prods.get(key, frozenset())):
                    src = self.output_type(p, key)
                    if src is None:
                        continue
                    reason = assignable(src, t)
                    if reason:
                        who = "the trigger input" if p == _TRIGGER else f"'{p}'"
                        self.out.error(
                            "V007",
                            loc,
                            f"input '{key}' ({format_type(t)}) from {who} ({format_type(src)}): {reason}",
                        )
            if signals_of(agent).normal:
                for key in field_types(agent.outputs):
                    written = self.output_type(agent_id, key)
                    others = sorted(prods.get(key, frozenset()) - {agent_id})
                    if not others:
                        continue
                    who = ", ".join(
                        "the trigger input" if p == _TRIGGER else f"'{p}'" for p in others
                    )
                    loc = self.loc(agent_id, "outputs", key)
                    self.out.warn(
                        "V009", loc, f"'{key}' is also written by {who} on a path to '{agent_id}'"
                    )
                    for p in others:
                        src = self.output_type(p, key)
                        if src is not None and written is not None and src != written:
                            self.out.error(
                                "V007",
                                loc,
                                f"'{key}' is re-written as {format_type(written)} but {('the trigger input' if p == _TRIGGER else repr(p))} wrote {format_type(src)}",
                            )
            self.check_compensator(agent)
        self.check_ends()

    def witness(self, agent_id: str, key: str) -> str:
        if agent_id == self.spec.entry and key not in self.trigger_types:
            return " — it is not a trigger input"
        for pred, signal in self.flow.preds.get(agent_id, []):
            pa = self.flow.available.get(pred, frozenset())
            produced = signal in signals_of(self.by_id[pred]).normal and key in field_types(
                self.by_id[pred].outputs_for(signal)
            )
            if key not in pa and not produced:
                return f" — missing when reached from '{pred}' via '{signal}'"
        return ""

    def check_compensator(self, agent: Any) -> None:
        if not agent.compensate or agent.compensate not in self.by_id:
            return
        comp = self.by_id[agent.compensate]
        supplied = {**field_types(agent.inputs)}
        for key in field_types(agent.outputs):
            exact = self.output_type(agent.id, key)
            if exact is not None:
                supplied[key] = exact
        for key, t in field_types(comp.inputs).items():
            loc = self.loc(comp.id, "inputs", key)
            if key not in supplied:
                if not t.optional:
                    self.out.error(
                        "V006",
                        loc,
                        f"compensator input '{key}' is neither an input nor an output of '{agent.id}'",
                    )
                continue
            reason = assignable(supplied[key], t)
            if reason:
                self.out.error(
                    "V007",
                    loc,
                    f"compensator input '{key}' ({format_type(t)}) from '{agent.id}': {reason}",
                )

    def check_ends(self) -> None:
        for end, output in self.spec.outputs.items():
            if end not in self.flow.end_available:
                continue
            avail = self.flow.end_available[end]
            prods = self.flow.end_producers.get(end, {})
            env: dict[str, Any] = {}
            for key in avail:
                for p in sorted(prods.get(key, frozenset())):
                    t = self.output_type(p, key)
                    if t is not None:
                        env[key] = t
                        break
            for loc, text in _strings(output.body, ("spec", "outputs", end, "body")):
                if "{{" not in text:
                    continue
                try:
                    template = compile_template(text)
                except TELError as exc:
                    self.out.error("V022", loc, _tel_message(exc))
                    continue
                missing = sorted(template.roots() - set(env))
                if missing:
                    self.out.error(
                        "V022",
                        loc,
                        f"{', '.join(repr(m) for m in missing)} is not produced on every path to END.{end}",
                    )
                    continue
                try:
                    template.check(env, self.cat.resolve)
                except (TELError, ValueError) as exc:
                    self.out.error("V022", loc, _tel_message(exc))
