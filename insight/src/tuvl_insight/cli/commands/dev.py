import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

import typer
from rich.console import Console
from rich.panel import Panel

from tuvl_insight.cli.session import (
    clear_session,
    generate_key,
    read_session,
    session_file,
    write_session,
)

console = Console()


def dev_cmd(
    port: int = typer.Option(8885, "--port", "-p", help="Port to bind the engine on."),
    project_dir: Path = typer.Option(
        None,
        "--project-dir",
        "-d",
        help="Path to the tuvl project directory (contains models/, workflows/, etc.). Defaults to current directory.",
        exists=True,
        file_okay=False,
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Skip opening the UI in a browser."
    ),
    allow_hosts: list[str] = typer.Option(
        [],
        "--allow-host",
        "-H",
        help=(
            "Allow remote connections from this IP or CIDR "
            "(e.g. --allow-host 192.168.1.0/24). Can be repeated. "
            "Localhost is always permitted."
        ),
    ),
    show_key: bool = typer.Option(
        False,
        "--show-key",
        help=(
            "Print the generated dev API key to the terminal. "
            "Off by default so the key is not captured by shell history, "
            "recording tools, or shared screens. The key is always "
            "persisted to <project-dir>/.tuvl/.dev-session (mode 0600)."
        ),
    ),
    auto_login: bool = typer.Option(
        False,
        "--auto-login",
        help="Automatically bypass the security screen in Tuvl Insight.",
    ),
    no_tui: bool = typer.Option(
        False,
        "--no-tui",
        help="Run the engine in this terminal as before, without the terminal UI that starts on a terminal.",
    ),
) -> None:
    """Run the engine in development mode: hot reload, the Insight editor at /insight and the terminal UI."""
    resolved = (project_dir or Path(".")).resolve()

    existing = read_session(resolved)
    if existing:
        console.print(
            Panel(
                f"A dev session is already running on port [bold]{existing['port']}[/bold] "
                f"(pid {existing['pid']}).\n"
                "Run [bold]tuvl dev --port[/bold] with a different port or stop the existing process first.",
                title="[yellow]Session already active[/yellow]",
                border_style="yellow",
            )
        )
        raise typer.Exit(1)

    key = generate_key()
    write_session(key, port, resolved)

    session_path = session_file(resolved)
    hosts_line = (
        f"\n[dim]Allowed remote hosts:[/dim] [cyan]{', '.join(allow_hosts)}[/cyan]"
        if allow_hosts
        else ""
    )
    if show_key:
        key_block = f"[bold yellow]Security key[/bold yellow]\n[bold cyan] {key} [/bold cyan]\n\n"
        key_hint = (
            "[dim]it will authenticate automatically.[/dim]"
            if auto_login
            else "[dim]paste the key above.[/dim]"
        )
    else:
        key_block = ""
        if auto_login:
            key_hint = (
                "[dim]it will authenticate automatically. The key is stored in [/dim]"
                f"[cyan]{session_path}[/cyan][dim] if needed.[/dim]"
            )
        else:
            key_hint = (
                "[dim]the security key is stored in [/dim]"
                f"[cyan]{session_path}[/cyan][dim] (run "
                "[/dim][bold]tuvl dev --show-key[/bold][dim] to print it).[/dim]"
            )
    console.print(
        Panel(
            f"Starting tuvl engine in [bold]dev mode[/bold] on port [bold]{port}[/bold].\n"
            f"Project: [bold]{resolved}[/bold]{hosts_line}\n\n"
            f"{key_block}"
            f"[dim]Open [/dim][cyan]http://127.0.0.1:{port}/insight/[/cyan][dim] and {key_hint}",
            title="[green]tuvl dev[/green]",
            border_style="green",
        )
    )

    bind_host = "0.0.0.0" if allow_hosts else "127.0.0.1"
    env = _build_env(key, port, resolved, allow_hosts, auto_login)
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "tuvl.main:app",
        "--host",
        bind_host,
        "--port",
        str(port),
        "--reload",
        "--reload-dir",
        str(resolved),
        "--reload-include",
        "*.yaml",
        *_reload_excludes(resolved),
    ]
    why_not = tui_unavailable(no_tui=no_tui, tty=sys.stdin.isatty() and sys.stdout.isatty())
    if why_not is None:
        _run_with_tui(argv, env, resolved, port, no_browser)
        return
    if why_not:
        console.print(f"[dim]{why_not}[/dim]")
    proc = subprocess.Popen(argv, env=env)

    if not no_browser:
        _open_ui_when_ready(port)

    def _shutdown(_sig: int, _frame: FrameType | None) -> None:  # noqa: ANN001
        console.print("\n[yellow]Shutting down dev session…[/yellow]")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        clear_session(resolved)
        raise typer.Exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    returncode = proc.wait()
    clear_session(resolved)
    if returncode != 0:
        raise typer.Exit(returncode)


def tui_unavailable(*, no_tui: bool, tty: bool) -> str | None:
    """``None`` when the terminal UI should start; otherwise why not (``""`` for the silent cases)."""
    if no_tui or not tty:
        return ""
    from tuvl.extras import available

    if not available("tui"):
        return "No terminal UI: install it with `uv tool install 'tuvl[standard]'` (or tuvl[tui]); running the engine here."
    return None


def _run_with_tui(argv: list[str], env: dict, resolved: Path, port: int, no_browser: bool) -> None:
    from tuvl_insight.tui.app import TuvlDevApp
    from tuvl_insight.tui.engine import Engine

    url = f"http://127.0.0.1:{port}/insight/"
    if not no_browser:
        import threading

        threading.Thread(target=_open_ui_when_ready, args=(port,), daemon=True).start()
    try:
        TuvlDevApp(resolved, engine=Engine(argv, env), insight_url=url).run()
    finally:
        clear_session(resolved)


def _build_env(
    key: str, port: int, project_dir: Path, allow_hosts: list[str], auto_login: bool
) -> dict:
    env = os.environ.copy()
    env["TUVL_PROJECT_DIR"] = str(project_dir)
    env["TUVL_DEV_MODE"] = "true"
    # Avoid the auth access-module layer-1 production-sentinel hard-fail:
    # TUVL_DEV_MODE=true paired with TUVL_ENV=production is *never* legal
    # (closes the leaked-dev-key footgun), so `tuvl dev` MUST declare a
    # dev environment.  ``setdefault`` so an explicit external override
    # like ``TUVL_ENV=production tuvl dev`` (security testing context)
    # still trips the sentinel as designed.
    env.setdefault("TUVL_ENV", "development")
    env["TUVL_ALLOW_DEV_AUTH"] = "true"
    env["TUVL_DEV_API_KEY"] = key
    env["TUVL_DEV_PORT"] = str(port)
    if auto_login:
        env["TUVL_DEV_AUTO_LOGIN"] = "1"
    if allow_hosts:
        env["TUVL_ALLOWED_HOSTS"] = ",".join(allow_hosts)
    return env


def _open_ui_when_ready(port: int) -> None:
    import webbrowser

    import httpx

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if resp.status_code == 200:
                webbrowser.open(f"http://127.0.0.1:{port}/insight/")
                return
        except Exception:
            pass
        time.sleep(0.5)


def _reload_excludes(project: Path) -> list[str]:
    """Runtime folders the hot reload must not watch: a coding agent's worktrees hold copies of the project's
    YAML, and every file it writes there would restart the server in the middle of the run.

    uvicorn only excludes folders that exist, so they are created first.
    """
    out: list[str] = []
    for rel in (
        ".tuvl/worktrees",
        ".tuvl/live",
        ".tuvl/audit",
        ".tuvl/trace",
        ".git",
        ".venv",
        "node_modules",
    ):
        folder = project / rel
        if rel.startswith(".tuvl/"):
            folder.mkdir(parents=True, exist_ok=True)
        if folder.is_dir():
            out += ["--reload-exclude", str(folder)]
    return out
