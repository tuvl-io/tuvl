"""``tuvl db`` subcommands for multi-tenant Row-Level Security maintenance.

Subcommands
-----------
* ``tuvl generate rls``  \u2014 print the idempotent RLS SQL to stdout (or a
  file via ``--out``).  Operators review and apply it through their normal
  migration tooling.
* ``tuvl verify rls``     \u2014 connect to the configured primary datasource
  and report which tenant tables are missing the
  ``tuvl_tenant_isolation`` policy.  Exits non-zero when policies are
  missing so it composes with CI / startup gates.

Both subcommands honour the active project's ``.tuvl/system.yaml`` locked
deployment mode \u2014 generation succeeds in any mode (useful for previewing
what would be emitted), but the bootstrap warning hint and the check
command are most meaningful under ``multi_tenant``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from tuvl.core.config import use_project

console = Console()

db_app = typer.Typer(
    name="db",
    help="Database maintenance subcommands (RLS policies, etc.).",
    no_args_is_help=True,
)


def _ensure_project_loaded(project_dir: Path | None) -> None:
    """Run the same load sequence the FastAPI lifespan performs (see
    ``tuvl.core.app.lifespan``) so the model/datasource registries are
    populated — and the ``tenant_id`` column injected on every model when the
    project is locked to ``multi_tenant`` — before we generate SQL or query
    policies.

    ``tuvl.core.config.settings`` is a module-level singleton built the first
    time ``tuvl.core.config`` is imported, which happens as a side effect of
    importing ``tuvl.cli`` (every sibling command module is imported eagerly
    by ``tuvl/cli/__init__.py``) — i.e. *before* this function ever runs. So
    setting ``TUVL_PROJECT_DIR`` alone would not change ``settings.project_dir``
    for an already-constructed singleton. We update the singleton's field
    in place (the same technique the test suite already uses via
    ``monkeypatch.setattr(settings, "tuvl_project_dir", ...)``) so every
    downstream consumer of ``settings.project_dir`` / ``settings.dir_for(...)``
    sees the requested project for the remainder of this process.
    """
    if project_dir is not None:
        resolved = str(project_dir.resolve())
        use_project(resolved)

        from tuvl.core.config import settings

        settings.tuvl_project_dir = resolved

    from tuvl.core.config_loader import load_all_configs

    load_all_configs()

    from tuvl.core.bootstrap import run_pre_flight

    asyncio.run(run_pre_flight())

    # ``check-rls`` queries the primary datasource for installed policies via
    # ``get_session_factory()``, which resolves against the RW/RO session
    # pools that ``init_db()`` normally builds during the app lifespan. We
    # deliberately call only the engine/session-factory step (``_build_engines``)
    # rather than the full ``init_db()`` — the latter also runs
    # ``create_all()`` across every registered model, which would make a
    # read-only SQL-preview/check command a schema-mutating one.
    from tuvl.core.datasources.postgres import _build_engines

    _build_engines()


@db_app.command("generate-rls")
def generate_rls(
    project_dir: Path = typer.Option(
        None,
        "--project-dir",
        "-d",
        exists=True,
        file_okay=False,
        help="Path to the tuvl project directory. Defaults to current directory.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Write the SQL to this file instead of stdout.",
    ),
) -> None:
    """Print idempotent RLS-enable + tenant-isolation policy SQL for every
    tenant-scoped table.

    Review the output and apply it through your normal migration tool
    (Alembic, raw psql, etc.).  Re-running the generator and re-applying
    the SQL is always safe.
    """
    # Resolve against the invoker's cwd *before* the project loads (which no
    # longer chdirs, but resolving early keeps `--out` unambiguous regardless).
    if out is not None:
        out = out.resolve()
    _ensure_project_loaded(project_dir)
    from tuvl.core.datasources.rls import generate_rls_sql, tenant_table_names

    tables = tenant_table_names()
    if not tables:
        console.print(
            "[yellow]No tenant-scoped tables found. "
            "Generate-rls is only meaningful after models are loaded.[/yellow]"
        )
        raise typer.Exit(code=1)

    sql = generate_rls_sql()
    if out is not None:
        out.write_text(sql)
        console.print(f"[green]\u2713[/green] Wrote {len(tables)} table(s) of RLS SQL to {out}")
    else:
        typer.echo(sql)


@db_app.command("check-rls")
def check_rls(
    project_dir: Path = typer.Option(
        None,
        "--project-dir",
        "-d",
        exists=True,
        file_okay=False,
        help="Path to the tuvl project directory. Defaults to current directory.",
    ),
) -> None:
    """Verify every tenant-scoped table has the ``tuvl_tenant_isolation``
    RLS policy installed.

    Exits 0 when every expected policy is present, and 1 (with a list of
    missing tables) otherwise.  Designed to run in CI before promoting a
    deployment to a multi-tenant environment.
    """
    _ensure_project_loaded(project_dir)
    import tuvl.core.bootstrap as _bootstrap

    if _bootstrap.LOCKED_DEPLOYMENT_MODE != "multi_tenant":
        console.print(
            Panel.fit(
                "Deployment mode is [bold]single_tenant[/bold].\n"
                "RLS policies are not required \u2014 nothing to check.",
                title="db check-rls",
                border_style="yellow",
            )
        )
        return

    from tuvl.core.datasources import rls

    async def checks() -> tuple[list[str], str | None]:
        return await rls.find_tables_missing_rls_policy(), await rls.primary_role_bypass_reason()

    missing, bypass = asyncio.run(checks())
    if not missing and not bypass:
        console.print(
            "[green]\u2713 All tenant-scoped tables have the tuvl_tenant_isolation policy installed.[/green]"
        )
        return

    problems = []
    if missing:
        problems.append(
            "[red]Missing RLS policies on:[/red]\n  - "
            + "\n  - ".join(missing)
            + "\n\nRun [bold]tuvl generate rls[/bold] and apply the SQL."
        )
    if bypass:
        problems.append(
            f"[red]The database role ignores RLS:[/red] {bypass}, so the policies protect nothing. "
            "Connect as a role that is neither a superuser nor BYPASSRLS."
        )
    console.print(Panel.fit("\n\n".join(problems), title="db check-rls", border_style="red"))
    raise typer.Exit(1)
