import re
import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth.access import bind_principal_context, require_access
from tuvl.core.auth.schemas import TokenUser
from tuvl.core.datasources.postgres import model_session
from tuvl.core.models import secure
from tuvl.core.models.schemas import SCHEMA_REGISTRY
from tuvl.core.repositories.registry import get_repository
from tuvl.core.system_config import expose_model_crud

logger = structlog.get_logger(__name__)

# Sentinel returned by _parse_include when the query param is absent.
_NO_INCLUDE: frozenset[str] = frozenset()

# Matches filter[field_name] bracket-notation query params.
_FILTER_RE = re.compile(r"^filter\[([A-Za-z_][A-Za-z0-9_]*)\]$")


def _constraint(exc: IntegrityError) -> HTTPException:
    """The answer to a write the database refused: the kind of constraint, never the values it names."""
    text = str(exc.orig).lower()
    if "unique" in text or "duplicate key" in text:
        return HTTPException(status_code=409, detail="A record with the same value already exists.")
    if "foreign key" in text:
        return HTTPException(
            status_code=409, detail="The record refers to, or is used by, another record."
        )
    if "not null" in text or "null value" in text:
        return HTTPException(status_code=422, detail="A required field is missing.")
    return HTTPException(
        status_code=409, detail="The change conflicts with the data already stored."
    )


def _norm_groups(value: Any) -> list[str] | None:
    """Normalize a spec.access group entry — bare string becomes a one-element list."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return [str(g) for g in value] or None


def _parse_filters(request: Request) -> dict[str, str] | None:
    """Extract ``filter[field]=value`` pairs from the query string.

    Example::

        ?filter[posting_id]=abc-123&filter[stage]=screening
        →  {"posting_id": "abc-123", "stage": "screening"}

    Returns ``None`` (not an empty dict) when no filter params are present so
    callers can distinguish "no filter" from "filter on zero columns".
    """
    result: dict[str, str] = {}
    for key, value in request.query_params.items():
        m = _FILTER_RE.match(key)
        if m:
            result[m.group(1)] = value
    return result or None


def _parse_include(include: str | None) -> frozenset[str]:
    """Convert a comma-separated ``?include=`` query string to a frozenset.

    Example:  "candidate,assessments"  →  frozenset({"candidate", "assessments"})
    """
    if not include:
        return _NO_INCLUDE
    return frozenset(s.strip() for s in include.split(",") if s.strip())


def _make_crud_router(model_name: str) -> APIRouter:
    schemas = SCHEMA_REGISTRY[model_name]
    CreateCls = schemas["create"]
    ReadCls = schemas["read"]
    UpdateCls = schemas["update"]
    ExpandedCls = schemas.get("expanded")  # None when no relations declared

    # ── Scope + group access control ──────────────────────────────────────
    # Scopes — convention: <modelnamelower>:read / :write / :delete,
    # overridable via spec.access.{read,write,delete}_scope in the model YAML.
    # Groups — spec.access.{read,write,delete}_groups pin the model to IAM
    # groups; scope AND group are both required, iam:admin bypasses. Absent
    # tiers cascade downward (write→read, delete→write) so declaring only
    # read_groups never leaves mutations open to a wider audience.
    access_cfg: dict = schemas.get("access") or {}
    _base = model_name.lower()
    _read_scope = str(access_cfg.get("read_scope") or f"{_base}:read")
    _write_scope = str(access_cfg.get("write_scope") or f"{_base}:write")
    _delete_scope = str(access_cfg.get("delete_scope") or f"{_base}:delete")
    _read_groups = _norm_groups(access_cfg.get("read_groups"))
    _write_groups = _norm_groups(access_cfg.get("write_groups")) or _read_groups
    _delete_groups = _norm_groups(access_cfg.get("delete_groups")) or _write_groups
    _auth_read = Depends(require_access(_read_scope, _read_groups))
    _auth_write = Depends(require_access(_write_scope, _write_groups))
    _auth_delete = Depends(require_access(_delete_scope, _delete_groups))

    enabled: frozenset[str] = schemas.get("crud") or frozenset()
    prefix = f"/models/{model_name.lower()}"
    # bind_principal_context authenticates every request and populates the
    # current_user_id / current_tenant_id ContextVars used by structlog,
    # OTel spans, and the multi-tenant DB session hook.
    router = APIRouter(
        prefix=prefix,
        tags=[model_name],
        dependencies=[Depends(bind_principal_context)],
    )

    # Single dependency instance shared by all routes for this model.
    # Automatically routes to the datasource declared in the model's YAML.
    _session = model_session(model_name)

    def _check_include(incl: frozenset[str], user: TokenUser) -> None:
        """An include embeds records of the related model, so the caller needs what that model asks of a
        reader (its read scope, and its read groups when it has them); ``iam:admin`` passes."""
        from tuvl.core.models.loader import MODEL_RELATIONS

        if "iam:admin" in user.scopes:
            return
        for rel in MODEL_RELATIONS.get(model_name, ()):
            if rel["name"] not in incl:
                continue
            related = rel["model"]
            related_access: dict = (SCHEMA_REGISTRY.get(related) or {}).get("access") or {}
            scope = str(related_access.get("read_scope") or f"{related.lower()}:read")
            groups = _norm_groups(related_access.get("read_groups"))
            if scope not in user.scopes or (groups and not set(groups) & set(user.groups)):
                needs = f"scope '{scope}'" + (f" and one of the groups {groups}" if groups else "")
                raise HTTPException(
                    status_code=403,
                    detail=f"Embedding '{rel['name']}' needs read access to {related}: {needs}.",
                )

    def _present(value: Any, user: TokenUser, cls: Any, incl: frozenset[str] = _NO_INCLUDE) -> Any:
        """*value* (a record, or a list of them) as *user* may see it: the ``secure`` fields they may not
        read are left out, in the record and in the related records an include embeds."""
        if not secure.needs_redaction(model_name, user.scopes, incl):
            return value
        rows = value if isinstance(value, list) else [value]
        dumped = [
            secure.strip_hidden(
                model_name, cls.model_validate(r).model_dump(by_alias=True), user.scopes
            )
            for r in rows
        ]
        return dumped if isinstance(value, list) else dumped[0]

    @router.post(
        "/",
        response_model=ReadCls,
        response_model_by_alias=True,
        response_model_exclude_unset=True,
        status_code=201,
        summary=f"Create a new {model_name}",
        dependencies=[_auth_write],
    )
    async def create(
        body: CreateCls,  # type: ignore[valid-type]
        session: AsyncSession = Depends(_session),
        user: TokenUser = _auth_write,
    ) -> Any:
        repo = get_repository(model_name, session)
        try:
            record = await repo.add(cast("Any", body).model_dump(exclude_none=False))
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _constraint(exc) from exc
        await session.refresh(record)
        return _present(record, user, ReadCls)

    # With relations declared the read endpoints answer with the expanded schema (the relations are
    # optional and only present when ?include= asked for them); answering with the plain read schema
    # dropped every embedded record.
    ReadOrExpanded = ExpandedCls or ReadCls

    @router.get(
        "/",
        response_model=list[ReadOrExpanded],  # type: ignore[valid-type]
        response_model_by_alias=True,
        response_model_exclude_unset=True,
        summary=f"List {model_name} records",
        dependencies=[_auth_read],
    )
    async def list_records(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        include: str | None = Query(
            default=None,
            description=(
                "Comma-separated relation names to embed. "
                "Example: `?include=candidate,assessments`. "
                "Triggers a 1-query-per-relation IN-clause batch — never N+1."
            ),
        ),
        session: AsyncSession = Depends(_session),
        user: TokenUser = _auth_read,
    ) -> Any:
        repo = get_repository(model_name, session)
        incl = _parse_include(include)
        criteria = _parse_filters(request)
        refused = sorted(set(criteria or ()) - repo.filterable_columns())
        if refused:
            raise HTTPException(status_code=422, detail=f"Cannot filter on: {', '.join(refused)}.")

        if incl and ExpandedCls is not None:
            _check_include(incl, user)
            rows = await repo.list_expanded(
                include=incl, criteria=criteria, limit=limit, offset=offset
            )
            return _present([ExpandedCls.model_validate(r) for r in rows], user, ExpandedCls, incl)

        return _present(
            await repo.list(criteria=criteria, limit=limit, offset=offset), user, ReadCls
        )

    @router.get(
        "/{record_id}",
        response_model=ReadOrExpanded,
        response_model_by_alias=True,
        response_model_exclude_unset=True,
        summary=f"Get a {model_name} by ID",
        dependencies=[_auth_read],
    )
    async def get_one(
        record_id: uuid.UUID,
        include: str | None = Query(
            default=None,
            description=(
                "Comma-separated relation names to embed. Example: `?include=candidate,posting`."
            ),
        ),
        session: AsyncSession = Depends(_session),
        user: TokenUser = _auth_read,
    ) -> Any:
        repo = get_repository(model_name, session)
        incl = _parse_include(include)

        if incl and ExpandedCls is not None:
            _check_include(incl, user)
            result = await repo.get_expanded(record_id, incl)
            if result is None:
                raise HTTPException(status_code=404, detail=f"{model_name} '{record_id}' not found")
            return _present(ExpandedCls.model_validate(result), user, ExpandedCls, incl)

        record = await repo.get(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"{model_name} '{record_id}' not found")
        return _present(record, user, ReadCls)

    @router.patch(
        "/{record_id}",
        response_model=ReadCls,
        response_model_by_alias=True,
        response_model_exclude_unset=True,
        summary=f"Partially update a {model_name}",
        dependencies=[_auth_write],
    )
    async def update(
        record_id: uuid.UUID,
        body: UpdateCls,  # type: ignore[valid-type]
        session: AsyncSession = Depends(_session),
        user: TokenUser = _auth_write,
    ) -> Any:
        repo = get_repository(model_name, session)
        # a field the body names is changed, even to null; one it leaves out is not touched
        patch_data = cast("Any", body).model_dump(exclude_unset=True)
        if not patch_data:
            raise HTTPException(status_code=422, detail="No fields provided to update")
        cleared = sorted(k for k, v in patch_data.items() if v is None and not repo.is_nullable(k))
        if cleared:
            raise HTTPException(status_code=422, detail=f"Cannot be null: {', '.join(cleared)}.")
        try:
            record = await repo.update(record_id, patch_data)
            if record is None:
                raise HTTPException(status_code=404, detail=f"{model_name} '{record_id}' not found")
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _constraint(exc) from exc
        await session.refresh(record)
        return _present(record, user, ReadCls)

    @router.delete(
        "/{record_id}",
        status_code=204,
        summary=f"Delete a {model_name}",
        dependencies=[_auth_delete],
    )
    async def delete(record_id: uuid.UUID, session: AsyncSession = Depends(_session)) -> Response:
        repo = get_repository(model_name, session)
        try:
            deleted = await repo.remove(record_id)
            if not deleted:
                raise HTTPException(status_code=404, detail=f"{model_name} '{record_id}' not found")
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _constraint(exc) from exc
        return Response(status_code=204)

    endpoint_ops: dict[Any, str] = {
        create: "create",
        list_records: "list",
        get_one: "read",
        update: "update",
        delete: "delete",
    }
    router.routes = [
        r
        for r in router.routes
        if (op := endpoint_ops.get(getattr(r, "endpoint", None))) is not None and op in enabled
    ]
    return router


def build_crud_routers(app: Any) -> None:
    if not expose_model_crud():
        logger.info(
            "CRUD routers disabled (spec.api.expose_model_crud / TUVL_EXPOSE_MODEL_CRUD)",
            models=len(SCHEMA_REGISTRY),
        )
        return
    if not SCHEMA_REGISTRY:
        logger.info("No schemas registered, no CRUD routers mounted")
        return

    mounted = 0
    for model_name, schemas in SCHEMA_REGISTRY.items():
        ops = schemas.get("crud") or frozenset()
        if not ops:
            continue  # spec.api.crud is empty: the model exposes no routes
        app.include_router(_make_crud_router(model_name))
        mounted += 1
        logger.info("CRUD router mounted", model=model_name.lower(), operations=sorted(ops))

    logger.info("CRUD routers built", count=mounted, models=len(SCHEMA_REGISTRY))
