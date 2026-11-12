import importlib.metadata
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    __version__ = importlib.metadata.version("tuvl")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"

from tuvl.cli import logo
from tuvl.cli.groups import RootGroup
from tuvl.extras import toolchain_installed

console = Console()

TOOLCHAIN_COMMANDS = ("init", "dev", "ship", "deploy")
_INSTALL = escape("uv tool install 'tuvl[standard]'")

app = typer.Typer(
    name="tuvl",
    help="tuvl — Intelligent Workflows. Deterministic Execution.",
    epilog=(
        f"Developer commands ({', '.join(TOOLCHAIN_COMMANDS)}) are part of the toolchain: "
        f"{_INSTALL}"
        if not toolchain_installed()
        else None
    ),
    invoke_without_command=True,
    cls=RootGroup,
)


def _register_toolchain() -> None:
    from tuvl_insight.cli.commands.deploy import deploy_cmd
    from tuvl_insight.cli.commands.dev import dev_cmd
    from tuvl_insight.cli.commands.init import init_cmd
    from tuvl_insight.cli.commands.ship import ship_cmd

    app.command("init", rich_help_panel="Setup")(init_cmd)
    app.command("dev", rich_help_panel="Run")(dev_cmd)
    app.command("ship", rich_help_panel="Release")(ship_cmd)
    app.command("deploy", rich_help_panel="Release")(deploy_cmd)


def _toolchain_hint(name: str) -> Callable[[], None]:
    def hint() -> None:
        Console(stderr=True).print(
            f"[red]`tuvl {name}` is part of the developer toolchain, which is not installed.[/red]\n"
            f"Install it with: [bold]{_INSTALL}[/bold]"
        )
        raise typer.Exit(2)

    return hint


if toolchain_installed():
    _register_toolchain()
else:
    for _name in TOOLCHAIN_COMMANDS:
        app.command(
            _name,
            hidden=True,
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        )(_toolchain_hint(_name))


from tuvl.cli import verbs  # noqa: E402 — registers the verb groups

verbs.register(app)


def _print_banner() -> None:
    commands_table = Table.grid(padding=(0, 2))
    commands_table.add_column(style="bold cyan", no_wrap=True)
    commands_table.add_column(style="white")
    commands_table.add_row("serve app", "Start the engine in production mode (uvicorn)")
    commands_table.add_row("run workflow", "Start a run of a workflow on a running server")
    commands_table.add_row("list runs", "List recent runs; also: show run, follow run, pin run")
    commands_table.add_row(
        "check system", "Check the machine and project setup: configuration, models, storage, git"
    )
    if toolchain_installed():
        commands_table.add_row("init", "Scaffold a new tuvl project")
        commands_table.add_row("import idea", "Record an idea; then: generate spec --idea …")
        commands_table.add_row("generate", "spec · tasks · code · ui-spec · lock · key · rls")
        commands_table.add_row("approve spec", "A person approves a spec (also: reject, reopen)")
        commands_table.add_row("lint", "lint project · lint spec — static rules, zero tokens")
        commands_table.add_row(
            "verify", "spec · lock · ui-spec · rls · audit — is it still in step?"
        )
        commands_table.add_row(
            "review code", "Code rules and ruff, optionally one spec or task only"
        )
        commands_table.add_row("run tests", "Run the project's tests (offline unless --live)")
        commands_table.add_row(
            "dev", "Start the engine in dev mode with hot-reload and the browser UI"
        )
        commands_table.add_row(
            "ship", "Validate, containerize, and emit a Helm chart for production"
        )
        commands_table.add_row("deploy", "Deploy to a tuvl cloud (cloud.tuvl.io) with a client token")
        commands_table.add_row(
            "list · show", "ideas, specs, tasks, runs, agents · idea, run, project, audit, drift"
        )
    else:
        commands_table.add_row("init, dev, generate, lint, …", f"Developer toolchain: {_INSTALL}")
    commands_table.add_row("help", "Show this help screen")

    description_md = Markdown(
        "Lightweight engine for building and deploying intelligent AI services "
        "using plain YAML workflow definitions. No boilerplate. No lock-in.\n\n"
        "**Docs:** https://tuvl.dev  •  **License:** Apache-2.0  •  "
        "**Status:** Stable — production-ready, versioned API & YAML schema"
    )

    header_group = logo.header(__version__, console.width)

    usage = Text.assemble(
        ("Usage: ", "dim"),
        ("tuvl", "bold bright_blue"),
        (" <command> ", "bold white"),
        ("[OPTIONS]", "dim"),
    )

    console.print(
        Panel(
            Group(
                header_group,
                description_md,
                Text(""),
                usage,
                Text(""),
                commands_table,
            ),
            border_style="bright_blue",
            padding=(1, 3),
            expand=False,
        )
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"tuvl v{__version__}")
        raise typer.Exit()


@app.callback()
def welcome(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show the current tuvl version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """tuvl — Intelligent Workflows. Deterministic Execution."""
    if ctx.invoked_subcommand is not None:
        return
    _print_banner()


@app.command("help")
def help_cmd() -> None:
    """Show the tuvl help screen with all available commands."""
    _print_banner()


def _command_words(argv: list[str]) -> str:
    """The verb and thing of this invocation (``generate code``, ``lint spec``): only words that are commands."""
    node: Any = typer.main.get_command(app)
    words: list[str] = []
    for token in argv:
        commands = getattr(node, "commands", None)
        if not commands or token not in commands:
            break
        words.append(token)
        node = commands[token]
    return " ".join(words)


def _project_of(argv: list[str]) -> Path:
    for i, token in enumerate(argv):
        if token in ("-d", "--project-dir") and i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
        if token.startswith("--project-dir="):
            return Path(token.split("=", 1)[1]).resolve()
    return Path.cwd()


def main() -> None:
    import time

    from tuvl.extras import MissingExtraError

    # developer telemetry and the coding-agent usage line belong to the toolchain; a bare install has neither
    devtelemetry: Any = None
    usage: Any = None
    if toolchain_installed():
        from tuvl_insight import devtelemetry
        from tuvl_insight.coding_agents.runner import usage

    started, code = time.perf_counter(), 0
    try:
        app()
    except MissingExtraError as exc:
        Console(stderr=True).print(f"[red]{escape(str(exc))}[/red]")
        code = 1
        raise SystemExit(1) from None
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        raise
    finally:
        if devtelemetry is not None and usage is not None:
            argv = sys.argv[1:]
            devtelemetry.record_command(
                _project_of(argv),
                _command_words(argv),
                exit_code=code,
                seconds=time.perf_counter() - started,
            )
            devtelemetry.shutdown()
            if usage.calls:
                Console(stderr=True).print(f"[dim]{escape(usage.line())}[/dim]")
