"""``tuvl iam`` — manage users, roles and scopes from inside the deployment.

Production serves no IAM administration over HTTP (the ``iam`` API group is not mounted), and it has
no Insight. An administrator runs this command where the database is reachable, for example::

    kubectl exec -it deploy/<app> -- tuvl iam roles list

It talks to the primary datasource directly and asks for an administrator's email and password first
(prompted, ``--password-stdin``, or ``TUVL_IAM_EMAIL`` / ``TUVL_IAM_PASSWORD``). The credentials say
who made a change; they are not a network boundary, because whoever can run the command already holds the
database credentials. ``reset-admin`` is the break-glass exception and asks for no login.

Roles come from the project's ``RoleSeed`` on a fresh database and are managed here afterwards.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from tuvl.extras import toolchain_installed

if TYPE_CHECKING:
    from tuvl.core.auth.models import IAMRole, IAMUser

console = Console()
err = Console(stderr=True)

iam_app = typer.Typer(
    name="iam",
    help="Manage users, roles and scopes where the database is reachable (kubectl exec).",
    no_args_is_help=True,
)
roles_app = typer.Typer(name="roles", help="Roles and who holds them.", no_args_is_help=True)
users_app = typer.Typer(name="users", help="Users.", no_args_is_help=True)
scopes_app = typer.Typer(name="scopes", help="Scopes the project enforces.", no_args_is_help=True)
iam_app.add_typer(roles_app, name="roles")
iam_app.add_typer(users_app, name="users")
iam_app.add_typer(scopes_app, name="scopes")

MIN_PASSWORD = 8


@dataclass
class _Opts:
    project_dir: Path
    email: str | None
    password_stdin: bool


@iam_app.callback()
def _common(
    ctx: typer.Context,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    email: Annotated[
        str | None,
        typer.Option(
            "--email", "-u", envvar="TUVL_IAM_EMAIL", help="Administrator email to sign in as."
        ),
    ] = None,
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read the administrator password from stdin (else TUVL_IAM_PASSWORD, else a prompt).",
        ),
    ] = False,
) -> None:
    ctx.obj = _Opts(project_dir, email, password_stdin)


# ── plumbing ──────────────────────────────────────────────────────────────────


def _svc() -> Any:
    """The IAM service, imported when a command runs: loading the CLI must not load the auth runtime."""
    from tuvl.core.auth import iam_service

    return iam_service


def _fail(message: str, code: int = 1) -> typer.Exit:
    err.print(f"[red]{message}[/red]")
    return typer.Exit(code)


def _admin_credentials(opts: _Opts) -> tuple[str, str]:
    import os

    email = opts.email or typer.prompt("Administrator email")
    if opts.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = os.environ.get("TUVL_IAM_PASSWORD") or typer.prompt("Password", hide_input=True)
    if not password:
        raise _fail("A password is required.")
    return email, password


def _new_password(what: str) -> str:
    import os

    password = os.environ.get("TUVL_IAM_NEW_PASSWORD") or typer.prompt(
        f"New password for {what}", hide_input=True, confirmation_prompt=True
    )
    if len(password) < MIN_PASSWORD:
        raise _fail(f"The password must be at least {MIN_PASSWORD} characters.")
    return password


def _load_project(project_dir: Path) -> None:
    """Load the project and open the datasource pools (no schema is created or changed)."""
    from tuvl.cli.commands.db import _ensure_project_loaded

    _ensure_project_loaded(project_dir.resolve())


def _load_config(project_dir: Path) -> None:
    """Load the project's documents only: no database is contacted."""
    from tuvl.core.config import settings, use_project
    from tuvl.core.config_loader import load_all_configs

    resolved = str(project_dir.resolve())
    use_project(resolved)
    settings.tuvl_project_dir = resolved
    load_all_configs()


def _execute[T](
    ctx: typer.Context,
    op: Callable[[Any, str | None], Awaitable[T]],
    *,
    auth: bool = True,
) -> T:
    """Load the project, sign the administrator in (unless *auth* is off) and run *op*."""
    opts: _Opts = ctx.find_object(_Opts)  # type: ignore[assignment]
    credentials = _admin_credentials(opts) if auth else None
    _load_project(opts.project_dir)

    async def main() -> T:
        from tuvl.core.datasources import postgres

        if postgres._PRIMARY_DS not in postgres._sessions_rw:
            raise _fail(
                "No primary PostgreSQL datasource is configured. Run this from the project "
                "directory (or pass --project-dir) with the database settings in the environment."
            )
        generator = postgres.get_primary_rw_session()
        session = await generator.__anext__()
        try:
            actor: str | None = None
            if credentials:
                admin = await _svc().authenticate_admin(
                    session, login_str=credentials[0], password=credentials[1]
                )
                actor = admin.email
            return await op(session, actor)
        finally:
            await generator.aclose()
            await postgres.close_db()

    try:
        return asyncio.run(main())
    except typer.Exit:
        raise
    except _svc().IamError as exc:
        raise _fail(str(exc) or exc.__class__.__name__) from None


def _audit(actor: str | None, action: str, target: str) -> None:
    from tuvl.core.logger import logger

    logger.info("auth.iam.cli", actor=actor or "(break-glass)", action=action, target=target)


async def _role(session: Any, name: str) -> IAMRole:
    role: IAMRole | None = await _svc().get_role_by_name(session, name)
    if role is None:
        raise _svc().NotFoundError(f"No role named '{name}'.")
    return role


async def _user(session: Any, email: str) -> IAMUser:
    user: IAMUser | None = await _svc().get_user_by_login(session, email)
    if user is None:
        raise _svc().NotFoundError(f"No user '{email}'.")
    return user


def _done(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


# ── roles ─────────────────────────────────────────────────────────────────────


@roles_app.command("list")
def roles_list(ctx: typer.Context) -> None:
    """List roles with their scopes and member counts."""

    async def op(session: Any, _actor: str | None) -> list[tuple[IAMRole, list[str], int]]:
        rows = []
        for role, scopes in await _svc().list_roles(session):
            members = await _svc()._role_members(session, role.id)
            rows.append((role, scopes, len(members)))
        return rows

    rows = _execute(ctx, op)
    table = Table("Role", "Active", "Members", "Scopes")
    for role, scopes, members in sorted(rows, key=lambda r: r[0].name):
        table.add_row(
            role.name,
            "yes" if role.is_active else "[yellow]disabled[/yellow]",
            str(members),
            ", ".join(sorted(scopes)) or "[dim]none[/dim]",
        )
    console.print(table)


@roles_app.command("create")
def roles_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Role name, e.g. recruiter.")],
    description: Annotated[
        str | None, typer.Option("--description", help="What the role is for.")
    ] = None,
    scope: Annotated[
        list[str] | None, typer.Option("--scope", "-s", help="A scope to grant (repeatable).")
    ] = None,
) -> None:
    """Create a role."""

    async def op(session: Any, actor: str | None) -> None:
        await _svc().create_role(
            session, name=name, description=description, scopes=list(dict.fromkeys(scope or []))
        )
        _audit(actor, "roles create", name)

    _execute(ctx, op)
    _done(f"Created role '{name}'.")


@roles_app.command("delete")
def roles_delete(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()],
    force: Annotated[
        bool, typer.Option("--force", help="Delete even when users still hold the role.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask to confirm.")] = False,
) -> None:
    """Delete a role (refused while it has members unless --force)."""
    if not yes and not typer.confirm(f"Delete role '{name}'?"):
        raise typer.Exit(1)

    async def op(session: Any, actor: str | None) -> None:
        role = await _role(session, name)
        await _svc().delete_role(session, role.id, require_empty=not force)
        _audit(actor, "roles delete", name)

    _execute(ctx, op)
    _done(f"Deleted role '{name}'.")


def _set_active(ctx: typer.Context, name: str, active: bool) -> None:
    async def op(session: Any, actor: str | None) -> None:
        role = await _role(session, name)
        await _svc().set_role_active(session, role.id, active)
        _audit(actor, "roles enable" if active else "roles disable", name)

    _execute(ctx, op)
    _done(f"Role '{name}' is {'enabled' if active else 'disabled'}.")


@roles_app.command("enable")
def roles_enable(ctx: typer.Context, name: Annotated[str, typer.Argument()]) -> None:
    """Enable a role again."""
    _set_active(ctx, name, True)


@roles_app.command("disable")
def roles_disable(ctx: typer.Context, name: Annotated[str, typer.Argument()]) -> None:
    """Disable a role: it keeps its members and scopes but grants nothing until enabled."""
    _set_active(ctx, name, False)


@roles_app.command("grant")
def roles_grant(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()],
    scope: Annotated[str, typer.Argument(help="e.g. candidate:read")],
) -> None:
    """Grant a scope to a role."""

    async def op(session: Any, actor: str | None) -> None:
        role = await _role(session, name)
        await _svc().grant_scope(session, role.id, scope)
        _audit(actor, "roles grant", f"{name} {scope}")

    _execute(ctx, op)
    _done(f"Role '{name}' grants '{scope}'.")


@roles_app.command("revoke")
def roles_revoke(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()],
    scope: Annotated[str, typer.Argument()],
) -> None:
    """Take a scope away from a role (its holders are signed out)."""

    async def op(session: Any, actor: str | None) -> None:
        role = await _role(session, name)
        await _svc().revoke_scope(session, role.id, scope)
        _audit(actor, "roles revoke", f"{name} {scope}")

    _execute(ctx, op)
    _done(f"Role '{name}' no longer grants '{scope}'.")


@roles_app.command("add-user")
def roles_add_user(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Role name.")],
    email: Annotated[str, typer.Argument(help="User email.")],
) -> None:
    """Give a user a role."""

    async def op(session: Any, actor: str | None) -> None:
        role = await _role(session, name)
        user = await _user(session, email)
        await _svc().assign_role(session, user.id, role.id)
        _audit(actor, "roles add-user", f"{name} {email}")

    _execute(ctx, op)
    _done(f"{email} holds '{name}'.")


@roles_app.command("remove-user")
def roles_remove_user(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Role name.")],
    email: Annotated[str, typer.Argument(help="User email.")],
) -> None:
    """Take a role away from a user (they are signed out)."""

    async def op(session: Any, actor: str | None) -> None:
        role = await _role(session, name)
        user = await _user(session, email)
        await _svc().revoke_role(session, user.id, role.id)
        _audit(actor, "roles remove-user", f"{name} {email}")

    _execute(ctx, op)
    _done(f"{email} no longer holds '{name}'.")


# ── users ─────────────────────────────────────────────────────────────────────


@users_app.command("list")
def users_list(ctx: typer.Context) -> None:
    """List users with their roles."""

    async def op(session: Any, _actor: str | None) -> list[tuple[IAMUser, list[str]]]:
        users: list[tuple[IAMUser, list[str]]] = await _svc().list_users(session)
        return users

    table = Table("Email", "Name", "Active", "Roles")
    for user, roles in sorted(_execute(ctx, op), key=lambda p: p[0].email):
        name = " ".join(p for p in (user.first_name, user.last_name) if p)
        table.add_row(
            user.email,
            name,
            "yes" if user.is_active else "[yellow]disabled[/yellow]",
            ", ".join(sorted(roles)) or "[dim]none[/dim]",
        )
    console.print(table)


@users_app.command("create")
def users_create(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument()],
    first_name: Annotated[str | None, typer.Option("--first-name")] = None,
    last_name: Annotated[str | None, typer.Option("--last-name")] = None,
    phone: Annotated[str | None, typer.Option("--phone")] = None,
    role: Annotated[
        list[str] | None, typer.Option("--role", "-r", help="A role to give (repeatable).")
    ] = None,
) -> None:
    """Create a user. The password is prompted (or TUVL_IAM_NEW_PASSWORD)."""
    password = _new_password(email)

    async def op(session: Any, actor: str | None) -> None:
        roles = [await _role(session, name) for name in dict.fromkeys(role or [])]
        user, _ = await _svc().create_user(
            session,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            phone_number=phone,
        )
        for held in roles:
            await _svc().assign_role(session, user.id, held.id)
        _audit(actor, "users create", email)

    _execute(ctx, op)
    _done(f"Created {email}.")


def _set_user_active(ctx: typer.Context, email: str, active: bool) -> None:
    async def op(session: Any, actor: str | None) -> None:
        user = await _user(session, email)
        await _svc().update_user(session, user.id, is_active=active)
        _audit(actor, "users enable" if active else "users disable", email)

    _execute(ctx, op)
    _done(f"{email} is {'enabled' if active else 'disabled'}.")


@users_app.command("enable")
def users_enable(ctx: typer.Context, email: Annotated[str, typer.Argument()]) -> None:
    """Let a disabled user sign in again."""
    _set_user_active(ctx, email, True)


@users_app.command("disable")
def users_disable(ctx: typer.Context, email: Annotated[str, typer.Argument()]) -> None:
    """Stop a user signing in (their sessions end)."""
    _set_user_active(ctx, email, False)


# ── scopes and project checks ─────────────────────────────────────────────────


def _project_scopes() -> dict[str, list[str]]:
    """Scope → what it guards, from the loaded project."""
    from tuvl.core.auth.scopes import scope_catalogue

    catalogue = scope_catalogue()
    guards: dict[str, list[str]] = {}
    for model, scopes in catalogue["crud"].items():
        for scope in scopes:
            guards.setdefault(scope, []).append(f"model {model}")
    for workflow, scopes in catalogue["workflows"].items():
        for scope in scopes:
            guards.setdefault(scope, []).append(f"workflow {workflow}")
    for scope in catalogue["system"]:
        guards.setdefault(scope, []).append("IAM administration")
    return guards


@scopes_app.command("list")
def scopes_list(ctx: typer.Context) -> None:
    """List the scopes this project enforces and what each guards."""
    opts: _Opts = ctx.find_object(_Opts)  # type: ignore[assignment]
    _load_config(opts.project_dir)
    table = Table("Scope", "Guards")
    for scope, guards in sorted(_project_scopes().items()):
        table.add_row(scope, ", ".join(sorted(guards)))
    console.print(table)


@iam_app.command("check")
def check_cmd(ctx: typer.Context) -> None:
    """Is every scope this project enforces granted by an active role? Exits 1 if not.

    Run it after a release that adds a scope: new routes answer 403 until a role grants it.
    """

    async def op(session: Any, _actor: str | None) -> tuple[dict[str, list[str]], set[str]]:
        granted: set[str] = set()
        for role, scopes in await _svc().list_roles(session):
            if role.is_active:
                granted.update(scopes)
        return _project_scopes(), granted  # the project is loaded by now

    guards, granted = _execute(ctx, op)
    missing = {s: g for s, g in guards.items() if s not in granted}
    if not missing:
        _done(f"All {len(guards)} scopes the project enforces are granted by an active role.")
        return
    table = Table("Scope with no active role", "Guards")
    for scope, where in sorted(missing.items()):
        table.add_row(scope, ", ".join(sorted(where)))
    err.print(table)
    err.print("Grant them with: tuvl iam roles grant <role> <scope>")
    raise typer.Exit(1)


@iam_app.command("reset-admin")
def reset_admin_cmd(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="Administrator to create or recover.")],
) -> None:
    """Break-glass: make EMAIL an active administrator with a new password. Asks for no login.

    Creates the user if missing, re-enables the account, and restores the superadmin role with
    iam:admin. Use it only when no administrator can sign in.
    """
    password = _new_password(email)

    async def op(session: Any, actor: str | None) -> None:
        await _svc().reset_admin(session, email=email, password=password)
        _audit(actor, "reset-admin", email)

    _execute(ctx, op, auth=False)
    _done(f"{email} is an active administrator.")


# ── export (developer toolchain) ──────────────────────────────────────────────


def export_cmd(
    ctx: typer.Context,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Write the live roles as a RoleSeed document for the next fresh database.

    Roles and scopes only: never users or passwords. Disabled roles are left out.
    """
    import yaml

    async def op(session: Any, _actor: str | None) -> list[dict[str, Any]]:
        roles = []
        for role, scopes in await _svc().list_roles(session):
            if not role.is_active:
                continue
            entry: dict[str, Any] = {"name": role.name}
            if role.description:
                entry["description"] = role.description
            if scopes:
                entry["scopes"] = sorted(scopes)
            roles.append(entry)
        return sorted(roles, key=lambda r: r["name"])

    from tuvl.core.config import settings

    # a developer's own database has no administrator to sign in as; production always asks
    roles = _execute(ctx, op, auth=settings.tuvl_env == "production")
    if not roles:
        raise _fail("There are no active roles to export.")
    text = yaml.safe_dump(
        {"kind": "RoleSeed", "spec": {"roles": roles}}, sort_keys=False, allow_unicode=True
    )
    if out is None:
        console.print(text, end="", markup=False, highlight=False)
    else:
        out.write_text(text)
        _done(f"Wrote {len(roles)} role(s) to {out}.")


if toolchain_installed():
    iam_app.command("export")(export_cmd)
else:
    from tuvl.cli.verbs import _hint

    iam_app.command(
        "export",
        hidden=True,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(_hint("iam export"))
