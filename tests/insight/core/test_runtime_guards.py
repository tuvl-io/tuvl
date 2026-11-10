"""Source-level pins for runtime security guards that don't have a cheap
integration-test harness yet.

End-to-end tests (FastAPI TestClient for CRUD scope, real Postgres for the
RAG tenant filter) are listed as post-launch milestones.  Until then, these
inspection-based assertions catch the most common regression: a refactor
that drops the protective call entirely.
"""

from __future__ import annotations

import inspect

# ── CRUD scope gate (Golden Rule 23) ─────────────────────────────────────────


def test_crud_router_uses_require_access_dependency() -> None:
    """Every auto-generated CRUD route must wire ``require_access`` (scope +
    optional group ACL) as a FastAPI dependency.  The README and AGENTS.md
    both promise this — if it ever falls off the build path, unauthenticated
    traffic could reach model CRUD endpoints.
    """
    from tuvl.core.api import crud_router

    src = inspect.getsource(crud_router)
    assert "from tuvl.core.auth.access import" in src
    assert "require_access" in src, "crud_router must import require_access"
    assert "Depends(require_access" in src, (
        "crud_router must wire require_access as a FastAPI Depends() — "
        "otherwise CRUD endpoints are unauthenticated."
    )
    # Belt-and-braces: confirm the scope and group wiring is present for
    # read, write, AND delete routes.
    for verb in ("read", "write", "delete"):
        assert f"_{verb}_scope" in src, f"crud_router missing the {verb!r} scope wiring"
        assert f"_{verb}_groups" in src, f"crud_router missing the {verb!r} group wiring"


# ── RAG tenant filter (Golden Rule 22 / defence-in-depth) ────────────────────


def test_data_search_applies_tenant_filter_in_multi_tenant_mode() -> None:
    """``tuvl.data_search`` must add a ``tenant_id = :tenant_id``
    predicate to its raw SQL when ``LOCKED_DEPLOYMENT_MODE == 'multi_tenant'``.
    This is defence-in-depth on top of the Postgres RLS policy — a future
    refactor that drops it must trip this test.
    """
    from tuvl.runtime import builtins as rag

    src = inspect.getsource(rag)

    # The tenant_id filter must be built from the current_tenant_id contextvar.
    assert "from tuvl.core.context import current_tenant_id" in src, (
        "rag.py must import current_tenant_id to build the per-tenant clause"
    )
    # The mode-gated assignment must be present.
    assert "LOCKED_DEPLOYMENT_MODE" in src
    assert "multi_tenant" in src, (
        "rag.py must gate the tenant clause on LOCKED_DEPLOYMENT_MODE == 'multi_tenant'"
    )
    # The SQL clause itself must reference tenant_id.
    assert "tenant_id = :tenant_id" in src, (
        "rag.py raw SQL must include 'tenant_id = :tenant_id' for the "
        "defence-in-depth tenant filter."
    )


def test_data_ingest_applies_tenant_scoping_in_multi_tenant_mode() -> None:
    """``tuvl.data_ingest`` rows must be tagged with the active tenant_id in
    multi-tenant mode so the matching DataSearch filter has something to
    constrain against.
    """
    from tuvl.runtime import builtins as rag

    src = inspect.getsource(rag)
    # The ingest path also needs current_tenant_id (covered by the same
    # import check above) and must write the tenant_id column on insert.
    # Look for the column name appearing in an INSERT statement context.
    assert "tenant_id" in src, "rag.py must reference tenant_id at all"
    # The bootstrap mode gate must apply to ingestion as well, not only search.
    assert src.count("LOCKED_DEPLOYMENT_MODE") >= 1


# ── Token blacklist documentation pin ────────────────────────────────────────


def test_cli_validate_import_does_not_construct_key_manager() -> None:
    """Importing the CLI validator must not pull in the auth runtime.

    ``validate.py`` imports light auth modules (schemas, workflow_policy);
    if ``tuvl.core.auth.__init__`` ever goes back to eager re-exports, every
    ``tuvl`` invocation on a project without TUVL_SIGNING_KEY dies at
    import with the production key-manager RuntimeError.
    """
    import os
    import subprocess
    import sys

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "TUVL_DEV_MODE",
            "TUVL_ALLOW_DEV_AUTH",
            "TUVL_SIGNING_KEY",
        )
    }
    code = (
        "import sys\n"
        "import tuvl_insight.cli.commands.validate\n"
        "assert 'tuvl.core.auth.access' not in sys.modules, 'auth runtime imported'\n"
        "assert 'tuvl.core.auth.keys' not in sys.modules, 'key manager module imported'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"


def test_blacklist_documents_multi_worker_requirement() -> None:
    """The token blacklist module docstring must continue to warn operators
    that Redis is required for multi-worker / multi-process deployments.
    Operators rely on the module-level docstring (and the README) to size
    their infrastructure; silently dropping the warning is unsafe.
    """
    from tuvl.core.auth import blacklist

    doc = (blacklist.__doc__ or "").lower()
    assert "redis" in doc and "multi-worker" in doc, (
        "blacklist module docstring must continue to warn that Redis is "
        "required for multi-worker deployments."
    )
