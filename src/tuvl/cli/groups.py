"""Command groups with a few extra manners."""

from __future__ import annotations

from typing import Any

from typer.core import TyperGroup

#: The order the top-level help lists commands, which is also the order of its panels.
ROOT_ORDER = (
    "init", "create", "import", "use", "set", "upgrade", "register", "update",
    "generate", "approve", "reject", "reopen", "renumber",
    "lint", "verify", "check", "review",
    "dev", "run", "serve", "follow", "pin", "replay",
    "list", "show",
    "ship", "deploy",
    "export", "prune", "iam", "help",
)  # fmt: skip


class RootGroup(TyperGroup):
    def list_commands(self, ctx: Any) -> list[str]:
        names = super().list_commands(ctx)
        rank = {name: i for i, name in enumerate(ROOT_ORDER)}
        return sorted(names, key=lambda n: (rank.get(n, len(rank)), n))
