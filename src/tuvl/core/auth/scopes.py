"""The scopes a project enforces: CRUD tiers per model, workflow ``required_scope``, and ``iam:admin``."""

from __future__ import annotations

from typing import Any


def scope_catalogue() -> dict[str, Any]:
    """Every scope an IAM role can be granted, grouped by what it guards.

    Needs the project loaded (models, schemas and workflows registered).
    """
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.models.loader import CRUD_OPERATIONS
    from tuvl.core.models.schemas import SCHEMA_REGISTRY
    from tuvl.core.system_config import expose_model_crud

    # ── CRUD model scopes ─────────────────────────────────────────────────
    # When the CRUD API is disabled the routes don't exist, so their scopes
    # are not listed — crud_api_enabled lets the IAM UI explain the gap.
    crud_enabled = expose_model_crud()
    crud: dict[str, list[str]] = {}
    if crud_enabled:
        for model_name, schemas in SCHEMA_REGISTRY.items():
            ops = schemas.get("crud") or frozenset()
            if not ops:
                continue
            access_cfg: dict = schemas.get("access") or {}
            base = model_name.lower()
            tiers = {CRUD_OPERATIONS[op] for op in ops}
            crud[model_name] = [
                str(access_cfg.get(f"{tier}_scope") or f"{base}:{tier}")
                for tier in ("read", "write", "delete")
                if tier in tiers
            ]

    # ── Workflow trigger scopes ───────────────────────────────────────────
    workflows: dict[str, list[str]] = {}
    for wf_name, doc in WORKFLOWS.items():
        if doc.metadata.required_scope:
            workflows[wf_name] = [doc.metadata.required_scope]

    return {
        "crud": crud,
        "crud_api_enabled": crud_enabled,
        "workflows": workflows,
        "system": ["iam:admin"],
    }
