"""``tuvl deploy``: ship this project to a tuvl cloud (cloud.tuvl.io) and deploy it, with a client token.

It runs ``tuvl ship --format archive`` (or takes an archive you made), uploads it, follows the cloud's upload, build and deploy, and prints the address. The
token comes from ``TUVL_CLIENT_TOKEN``, standard input or a saved login, never from an argument. See :mod:`tuvl_insight.cli.cloud`.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from tuvl_insight.cli import cloud

console = Console()
err = Console(stderr=True)


def _fail(message: str, code: int = 1) -> typer.Exit:
    err.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code)


def _ship(
    project_dir: Path, out: Path, *, allow_dirty: bool, allow_incomplete: bool, strict: bool
) -> Path:
    """Package the project as an archive with `tuvl ship`, the same command you would run yourself."""
    cmd = [
        sys.executable,
        "-c",
        "from tuvl.cli.main import main; main()",
        "ship",
        "--format",
        "archive",
        "--archive-format",
        "tar.gz",
        "--out",
        str(out),
        "-d",
        str(project_dir),
    ]
    cmd += [
        flag
        for flag, on in (
            ("--allow-dirty", allow_dirty),
            ("--allow-incomplete", allow_incomplete),
            ("--strict", strict),
        )
        if on
    ]
    console.print("[bold]Packaging[/bold] the project with `tuvl ship --format archive`...")
    done = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603 - our own interpreter and arguments
    if done.returncode != 0:
        err.print((done.stdout + done.stderr)[-2000:])
        raise _fail(
            "`tuvl ship` did not succeed, so nothing was uploaded. Fix what it reports and run `tuvl deploy` again."
        )
    found = sorted(out.glob("*.tar.gz"))
    if not found:
        raise _fail("`tuvl ship` made no archive, so nothing was uploaded.")
    return found[-1]


def deploy_cmd(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        "-d",
        help="Path to the tuvl project.",
        exists=True,
        file_okay=False,
    ),
    archive: Path | None = typer.Option(
        None,
        "--archive",
        help="Deploy this archive (made by `tuvl ship --format archive`) instead of packaging the project.",
        exists=True,
        dir_okay=False,
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        help=f"The cloud to deploy to (default: ${cloud.URL_ENV}, then {cloud.CLOUD_DEFAULT}).",
    ),
    plan: str = typer.Option(
        "hobby",
        "--plan",
        help="The hosting plan for a new project (hobby or standard); its price is taken from your wallet.",
    ),
    hostname: str | None = typer.Option(
        None,
        "--hostname",
        help="The name to serve a new deployment on (default: <project>.<the cloud's base domain>).",
    ),
    set_hostname: str | None = typer.Option(
        None,
        "--set-hostname",
        help="Change the name the deployed app is served on, and nothing else: nothing is packaged or uploaded. The app restarts behind the new name; the old name stops working.",
    ),
    check_dns: bool = typer.Option(
        False,
        "--check-dns",
        help="Check that a name is available and points at the cloud (the name of --hostname or --set-hostname, else the project's default name). Nothing is deployed.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="The project to check or rename (default: the name in pyproject.toml).",
    ),
    admin_email: str | None = typer.Option(
        None,
        "--admin-email",
        envvar="TUVL_ADMIN_EMAIL",
        help="Create an administrator with this email in the new app; its password is made here, shown once, and kept nowhere.",
    ),
    no_admin: bool = typer.Option(
        False, "--no-admin", help="Do not create an administrator in the app."
    ),
    login: bool = typer.Option(
        False,
        "--login",
        help="Save a client token for this cloud (asked for with a hidden prompt) and exit.",
    ),
    logout: bool = typer.Option(
        False, "--logout", help="Forget the saved token for this cloud and exit."
    ),
    token_stdin: bool = typer.Option(
        False,
        "--token-stdin",
        help="Read the client token from standard input (for CI). It is never taken from an argument.",
    ),
    no_follow: bool = typer.Option(
        False, "--no-follow", help="Start the deployment and return; it continues on the cloud."
    ),
    allow_dirty: bool = typer.Option(
        False,
        "--allow-dirty",
        help="Pass to `tuvl ship`: package although the working tree has uncommitted changes.",
    ),
    allow_incomplete: bool = typer.Option(
        False,
        "--allow-incomplete",
        help="Pass to `tuvl ship`: package although some spec tasks are not done.",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Pass to `tuvl ship`: treat validation warnings as errors."
    ),
) -> None:
    """Deploy this project to a tuvl cloud with a client token (cloud.tuvl.io by default)."""
    for noisy in ("httpx", "httpcore"):  # one line per request would bury the steps this prints
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        base = cloud.cloud_url(url)
    except cloud.CloudError as exc:
        raise _fail(str(exc), 2) from None
    if login or logout:
        raise _login_or_logout(base, login=login, token_stdin=token_stdin)
    piped = sys.stdin.read().strip() if token_stdin else None
    try:
        found = cloud.resolve_token(base, stdin_token=piped)
    except cloud.CloudError as exc:
        raise _fail(str(exc), 2) from None
    if found is None:
        raise _fail(
            f"No client token for {base}. Make one in the cloud's console (Customers, Deploy tokens), then either `export {cloud.TOKEN_ENV}=...` or run `tuvl deploy --login`."
        )
    token, source = found
    if admin_email and no_admin:
        raise _fail("--admin-email and --no-admin cannot be used together.", 2)
    if set_hostname and (archive or hostname or admin_email):
        raise _fail(
            "--set-hostname only renames the deployed app; it cannot be combined with --archive, --hostname or --admin-email.",
            2,
        )
    if check_dns and (archive or admin_email):
        raise _fail(
            "--check-dns only checks a name; it cannot be combined with --archive or --admin-email.",
            2,
        )
    try:
        api = cloud.Cloud(base, token)
        me = api.me()
        console.print(
            f"Deploying to [bold]{escape(base)}[/bold] as [bold]{escape(me.get('label', ''))}[/bold] for {escape(me.get('customer', ''))} (token from {source})."
        )
        wallet = me.get("wallet")
        if wallet is not None:
            console.print(f"Wallet: {wallet:,.0f} credits.")
        if check_dns or set_hostname:
            slug = project or _project_slug(project_dir)
            if check_dns:
                ok = _print_check(api.check_hostname(set_hostname or hostname or "", slug))
                raise typer.Exit(0 if ok else 1)
            new_name = (set_hostname or "").strip().lower()
            console.print(
                f"Changing the name of [bold]{escape(slug)}[/bold] to [bold]{escape(new_name)}[/bold]..."
            )
            job = _follow(api, api.set_hostname(slug, new_name))
            _finish(job, what="rename")
            return
        email = "" if no_admin else (admin_email or "").strip()
        with tempfile.TemporaryDirectory(prefix="tuvl-deploy-") as scratch:
            built = (
                archive
                if archive
                else _ship(
                    project_dir,
                    Path(scratch),
                    allow_dirty=allow_dirty,
                    allow_incomplete=allow_incomplete,
                    strict=strict,
                )
            )
            slug = _slug_of(built)
            if (
                not email
                and not no_admin
                and slug
                and slug not in {p["project"] for p in me.get("projects", [])}
                and sys.stdin.isatty()
                and sys.stdout.isatty()
            ):
                email = typer.prompt(
                    f"Administrator email for the new app {slug} (leave empty to skip)",
                    default="",
                    show_default=False,
                ).strip()
            password = envelope = ""
            if email:
                key = api.seal_key()
                password = cloud.generate_password()
                envelope = cloud.seal(key["public_key"], key["key_id"], password, slug=slug or "")
            console.print(
                f"Uploading [bold]{built.name}[/bold] ({built.stat().st_size / 1024:,.0f} KB)..."
            )
            started = api.start(
                built,
                plan=plan,
                hostname=hostname or "",
                admin_email=email,
                admin_envelope=envelope,
            )
        console.print(
            f"Deployment [bold]{started['id'][:8]}[/bold] for project [bold]{escape(started.get('project', ''))}[/bold] started."
        )
        if no_follow:
            console.print(
                "Follow it with the cloud's console, or run `tuvl deploy` again; it keeps going on the cloud."
            )
            return
        job = _follow(api, started)
    except cloud.CloudError as exc:
        raise _fail(str(exc)) from None
    except subprocess.SubprocessError as exc:
        raise _fail(f"could not run `tuvl ship`: {exc}") from None
    result = _finish(job, what="deployment")
    lines = [
        f"[green bold]Deployed.[/green bold]  [bold]{escape(result.get('url') or result.get('hostname', ''))}[/bold]",
        f"Project {escape(result.get('project', ''))} on the {escape(result.get('plan', ''))} plan",
    ]
    admin_state = result.get("admin", "")
    if email and admin_state == "set":
        lines += [
            "",
            f"Administrator created: [bold]{escape(email)}[/bold]",
            f"Password: [bold]{password}[/bold]",
            "[yellow]Save the password now. It is shown only here, once; the cloud keeps no copy.[/yellow]",
        ]
    elif email:
        lines += [
            "",
            f"[yellow]The app is running but its administrator could not be created ({escape(admin_state or 'no answer')}). Ask the cloud's operator to reset it.[/yellow]",
        ]
    lines += ["", "[dim]Check the name any time: tuvl deploy --check-dns[/dim]"]
    console.print(Panel("\n".join(lines), title="[green]tuvl deploy[/green]", border_style="green"))


def _follow(api: cloud.Cloud, started: dict) -> dict:
    try:
        return api.follow(
            started["id"],
            lambda name, detail: console.print(
                f"  [cyan]{name}[/cyan]" + (f"  {escape(detail)}" if detail else "")
            ),
        )
    except cloud.CloudError as exc:
        raise _fail(str(exc)) from None


def _finish(job: dict, *, what: str) -> dict:
    """The result of a job that succeeded, or exit 1 with its reason. A rename ends here with its own panel."""
    if job.get("state") != "succeeded":
        raise _fail(f"The {what} failed: {job.get('message') or 'no reason was given'}")
    result: dict[str, Any] = job.get("result", {})
    if what == "rename":
        console.print(
            Panel(
                f"[green bold]Renamed.[/green bold]  [bold]{escape(result.get('url') or result.get('hostname', ''))}[/bold]\nProject {escape(result.get('project', ''))} is now served as {escape(result.get('hostname', ''))}. "
                "The old name no longer works.\n\n[dim]Check the name: tuvl deploy --check-dns[/dim]",
                title="[green]tuvl deploy[/green]",
                border_style="green",
            )
        )
        raise typer.Exit(0)
    return result


def _project_slug(project_dir: Path) -> str:
    """The project's name as `tuvl ship` would read it from pyproject.toml."""
    from tuvl_insight.cli.commands.ship import _project_meta

    return _project_meta(project_dir)[0]


def _print_check(report: dict) -> bool:
    """Say whether the name is usable and whether it points at the cloud. True when the name is available (or already this project's)."""
    avail, dns = report.get("availability", {}), report.get("dns")
    status = avail.get("status", "")
    usable = status in ("available", "yours")
    mark = "[green]✓[/green]" if usable else "[red]✗[/red]"
    console.print(
        f"{mark} [bold]{escape(report.get('hostname', ''))}[/bold]: {escape(avail.get('message', status))}"
    )
    if dns:
        tone = {"ok": "green", "missing": "yellow", "wrong": "yellow", "no-address": "yellow"}.get(
            dns.get("status", ""), "yellow"
        )
        console.print(
            f"  DNS: [{tone}]{escape(dns.get('status', ''))}[/{tone}]  {escape(dns.get('message', ''))}"
        )
    elif usable:
        console.print("  DNS: not checked")
    return usable


def _slug_of(archive: Path) -> str:
    """The project name inside an archive (BUILD-INFO.json), or "" when it cannot be read; used only to decide whether to ask for an administrator."""
    import json
    import tarfile

    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if (
                    member.isfile()
                    and member.name.rsplit("/", 1)[-1] == "BUILD-INFO.json"
                    and member.name.count("/") <= 1
                ):
                    handle = tar.extractfile(member)
                    return (
                        str(json.loads(handle.read(65536)).get("project") or "") if handle else ""
                    )
    except (OSError, tarfile.TarError, ValueError):
        return ""
    return ""


def _login_or_logout(base: str, *, login: bool, token_stdin: bool) -> typer.Exit:
    if not login:
        console.print(
            f"Forgot the saved token for {escape(base)}."
            if cloud.forget_token(base)
            else f"There was no saved token for {escape(base)}."
        )
        return typer.Exit(0)
    raw = sys.stdin.read() if token_stdin else typer.prompt("Client token", hide_input=True)
    try:
        token = cloud.check_token(raw)
        me = cloud.Cloud(base, token).me()  # prove it works before saving it
        cloud.store_token(base, token, label=me.get("label", ""))
    except cloud.CloudError as exc:
        return _fail(str(exc))
    console.print(
        f"Saved. [bold]{escape(me.get('label', ''))}[/bold] for {escape(me.get('customer', ''))} on {escape(base)}."
    )
    return typer.Exit(0)
