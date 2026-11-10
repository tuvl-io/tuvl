"""``tuvl deploy`` — managed hosting is planned; for now the command only says so."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()

MESSAGE = (
    "[bold]Managed hosting is coming soon.[/bold]\n\n"
    "[bold]tuvl deploy[/bold] will publish this project to a hosted, managed tuvl engine that "
    "runs your backend services, so there is no image or cluster to operate.\n\n"
    "It is not available yet, and nothing was deployed.\n\n"
    "Until then, build and deploy it yourself:\n"
    "  [bold]tuvl ship[/bold]   validate, then write a Dockerfile and a Helm chart\n"
    "  https://tuvl.dev/getting-started/shipping/\n\n"
    "Follow the launch at https://tuvl.io"
)


def deploy_cmd(
    project_dir: Path = typer.Option(
        Path("."), "--project-dir", "-d", help="Path to the tuvl project (not used yet)."
    ),
) -> None:
    """Deploy to managed tuvl hosting (coming soon: prints a message and exits 1)."""
    console.print(Panel(MESSAGE, title="[blue]tuvl deploy[/blue]", border_style="blue"))
    raise typer.Exit(1)  # nothing was deployed: a script must not read this as success
