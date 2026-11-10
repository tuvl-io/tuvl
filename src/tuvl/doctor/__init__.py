"""``tuvl check system``: named groups of checks over a project, each ok / warn / fail / skip.

A group is a function registered with :func:`group`; the ones marked ``toolchain`` appear only
where the developer toolchain is installed. A check may carry a :class:`Fix` — a safe,
non-destructive repair that ``tuvl check system --fix`` applies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Status = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True)
class Fix:
    label: str
    apply: Callable[[], object]


@dataclass
class Check:
    group: str
    id: str
    status: Status
    detail: str = ""
    hint: str = ""
    fix: Fix | None = None


@dataclass
class Context:
    project: Path
    live: bool = False
    #: what one group leaves for a later one or for the JSON report
    data: dict[str, Any] = field(default_factory=dict)


GroupFn = Callable[[Context], list[Check]]


@dataclass(frozen=True)
class Group:
    name: str
    fn: GroupFn
    toolchain: bool = False


GROUPS: dict[str, Group] = {}


def group(name: str, *, toolchain: bool = False) -> Callable[[GroupFn], GroupFn]:
    def register(fn: GroupFn) -> GroupFn:
        GROUPS[name] = Group(name, fn, toolchain)
        return fn

    return register


def _register_groups() -> bool:
    """Register the built-in groups, and the toolchain's when it is installed. Returns whether it is."""
    from tuvl.extras import toolchain_installed

    from . import groups as _groups  # noqa: F401

    installed = toolchain_installed()
    if installed:
        from tuvl_insight import doctor_groups as _toolchain_groups  # noqa: F401
    return installed


def available_groups() -> list[str]:
    installed = _register_groups()
    return [g.name for g in GROUPS.values() if installed or not g.toolchain]


def run(ctx: Context, names: list[str]) -> list[Check]:
    _register_groups()
    out: list[Check] = []
    for name in names:
        try:
            out.extend(GROUPS[name].fn(ctx))
        except Exception as exc:
            out.append(
                Check(
                    name,
                    "group",
                    "fail",
                    f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}",
                )
            )
    return out


@dataclass
class Summary:
    ok: int = 0
    warn: int = 0
    fail: int = 0
    skip: int = 0
    by_group: dict[str, dict[str, int]] = field(default_factory=dict)


def summarise(checks: list[Check]) -> Summary:
    total = Summary()
    for c in checks:
        setattr(total, c.status, getattr(total, c.status) + 1)
        per = total.by_group.setdefault(c.group, {"ok": 0, "warn": 0, "fail": 0, "skip": 0})
        per[c.status] += 1
    return total
