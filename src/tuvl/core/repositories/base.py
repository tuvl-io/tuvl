from __future__ import annotations

import datetime as _dt
import uuid as _uuid_mod
from collections.abc import Set as AbstractSet
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as _sa
from sqlalchemy import and_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.models.loader import MODEL_REGISTRY, MODEL_RELATIONS

# ``BaseRepository.list`` shadows the builtin inside the class body, so annotations there use these.
_Rows = list[dict[str, Any]]
_Records = list[Any]


class BaseRepository[T]:
    def __init__(self, model_name: str, session: AsyncSession) -> None:
        self.session = session
        self.model_name = model_name
        self.model_class: type[T] = MODEL_REGISTRY.get(model_name)  # type: ignore[assignment]

        if not self.model_class:
            raise ValueError(
                f"Repository Error: Model '{model_name}' not found in MODEL_REGISTRY. "
                f"Available: {list(MODEL_REGISTRY.keys())}"
            )

    @property
    def _table(self) -> _sa.Table:
        table: _sa.Table = cast("Any", self.model_class).__table__
        return table

    async def add(self, entity_data: dict[str, Any]) -> T:
        # Table-model SQLModel classes skip pydantic coercion, so ISO strings for
        # date/datetime/uuid/numeric columns would reach asyncpg untyped and fail.
        # Coerce write values against the SA column types first.
        col_map = {col.name: col for col in self._table.columns}
        entity_data = {
            key: (_coerce_write_value(col_map[key], val) if key in col_map else val)
            for key, val in entity_data.items()
        }
        db_obj = self.model_class(**entity_data)
        self.session.add(db_obj)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def get(self, ident: Any) -> T | None:
        return await self.session.get(self.model_class, ident)

    def is_nullable(self, name: str) -> bool:
        """Whether *name* may be null (a name that is not a column is left for the update to refuse)."""
        column = self._table.columns.get(name)
        return column is None or bool(column.nullable)

    def filterable_columns(self) -> frozenset[str]:
        """Columns a ``filter[...]`` may name: the table's, minus ``secure`` fields (filtering on one
        would let a caller test guesses against a value it may not read, so it is refused for every
        caller, whether or not they hold ``<model>:read_secure``)."""
        from tuvl.core.models.loader import secure_fields_view

        secure = secure_fields_view()
        return frozenset(c.name for c in self._table.columns if c.name not in secure)

    async def list(
        self, criteria: dict[str, Any] | None = None, limit: int = 100, offset: int = 0
    ) -> list[T]:
        statement = select(self.model_class)

        if criteria:
            col_map = {col.name: col for col in self._table.columns}
            filters = []
            for col_name, val in criteria.items():
                col = col_map.get(col_name)
                if col is None:
                    continue
                # Coerce raw string values (from query params) to the column's native type.
                if isinstance(val, str):
                    val = _coerce_filter_value(col, val)
                filters.append(getattr(self.model_class, col_name) == val)
            if filters:
                statement = statement.where(and_(*filters))

        # a stable order: without one, pages of a table can repeat rows or skip them
        statement = statement.order_by(*self._table.primary_key.columns).offset(offset).limit(limit)
        result = await self.session.exec(statement)
        return list(result.all())

    async def update(self, ident: Any, data: dict[str, Any]) -> T | None:
        obj = await self.get(ident)
        if obj is None:
            return None

        col_map = {col.name: col for col in self._table.columns}
        for key, value in data.items():
            if hasattr(obj, key):
                if key in col_map:
                    value = _coerce_write_value(col_map[key], value)
                setattr(obj, key, value)

        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def remove(self, ident: Any) -> bool:
        obj = await self.get(ident)
        if obj is None:
            return False

        await self.session.delete(obj)
        await self.session.flush()
        return True

    async def get_expanded(self, ident: Any, include: AbstractSet[str]) -> dict[str, Any] | None:
        """Fetch a single record and resolve requested relations.

        Each included relation costs exactly 1 extra query (SELECT … WHERE id = ?
        for many_to_one, or SELECT … WHERE fk = ? for one_to_many), for a total
        of 1 + len(include) queries — never N+1.
        """
        record = await self.get(ident)
        if record is None:
            return None

        result: dict[str, Any] = _orm_to_dict(record)
        await self._attach_relations([result], [record], include)
        return result

    async def list_expanded(
        self,
        include: AbstractSet[str],
        criteria: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> _Rows:
        """Fetch a page of records and resolve relations using IN-clause batching.

        Query count is always  1 (base list) + 1 per included relation,
        regardless of page size.  No N+1 possible.
        """
        records = await self.list(criteria=criteria, limit=limit, offset=offset)
        if not records:
            return []

        results: _Rows = [_orm_to_dict(r) for r in records]
        await self._attach_relations(results, records, include)
        return results

    async def _attach_relations(
        self,
        results: _Rows,
        records: _Records,
        include: AbstractSet[str],
    ) -> None:
        """Mutate *results* in-place, injecting related data for every relation
        name that appears in *include*.

        Batching strategy
        -----------------
        many_to_one  (FK on this model, e.g. application.candidate_id)
            1. Collect the set of FK values from all base records.
            2. Emit:  SELECT … WHERE id IN (<fk_values>)
            3. Build a lookup dict  {related_pk: related_dict}.
            4. Assign into each result row.

        one_to_many  (FK on related model, e.g. assessment.application_id)
            1. Collect the set of base PKs.
            2. Emit:  SELECT … WHERE <fk_col> IN (<base_pks>)
            3. Group rows by FK value  {base_pk: [related_dict, ...]}.
            4. Assign into each result row.
        """
        relations = MODEL_RELATIONS.get(self.model_name, [])
        if not relations:
            return

        # Locate the primary-key column name on this model class.
        this_pk = _pk_col(self.model_class)

        for rel in relations:
            if rel["name"] not in include:
                continue

            rel_model_cls = MODEL_REGISTRY.get(rel["model"])
            if rel_model_cls is None:
                continue  # related model not loaded — skip silently

            fk = rel["foreign_key"]

            if rel["type"] == "many_to_one":
                fk_values = {
                    getattr(rec, fk) for rec in records if getattr(rec, fk, None) is not None
                }
                if not fk_values:
                    for row in results:
                        row[rel["name"]] = None
                    continue

                rel_pk = _pk_col(rel_model_cls)
                rel_pk_attr = getattr(rel_model_cls, rel_pk)
                stmt = select(rel_model_cls).where(rel_pk_attr.in_(fk_values))
                db_result = await self.session.exec(stmt)
                lookup: dict[Any, dict] = {
                    getattr(r, rel_pk): _orm_to_dict(r) for r in db_result.all()
                }

                for row, rec in zip(results, records, strict=False):
                    row[rel["name"]] = lookup.get(getattr(rec, fk))

            elif rel["type"] == "one_to_many":
                base_pks = {getattr(rec, this_pk) for rec in records}
                fk_attr = getattr(rel_model_cls, fk)
                stmt = select(rel_model_cls).where(fk_attr.in_(base_pks))
                db_result = await self.session.exec(stmt)

                grouped: dict[Any, list[dict]] = {}
                for r in db_result.all():
                    key = getattr(r, fk)
                    grouped.setdefault(key, []).append(_orm_to_dict(r))

                for row, rec in zip(results, records, strict=False):
                    row[rel["name"]] = grouped.get(getattr(rec, this_pk), [])


def _orm_to_dict(obj: Any) -> dict[str, Any]:
    """Convert an SQLModel/SQLAlchemy ORM instance to a plain dict."""
    return {col.name: getattr(obj, col.name) for col in obj.__class__.__table__.columns}


def _pk_col(model_cls: Any) -> str:
    """Return the name of the first primary-key column."""
    for col in model_cls.__table__.columns:
        if col.primary_key:
            return str(col.name)
    raise ValueError(f"No primary key found on {model_cls.__name__}")


def _coerce_write_value(col: Any, val: Any) -> Any:
    """Coerce an ISO string write value to the column's native Python type.

    Table-model SQLModel classes bypass pydantic validation, so a JSON string
    like ``"2026-06-01"`` for a ``Date`` column would reach asyncpg as a ``str``
    and raise ``'str' object has no attribute 'toordinal'``. Non-string values
    and unmatched types pass through unchanged.
    """
    if not isinstance(val, str):
        return val
    try:
        col_type = col.type
        # DateTime must be checked before Date (they are sibling SA types).
        if isinstance(col_type, _sa.DateTime):
            return _dt.datetime.fromisoformat(val)
        if isinstance(col_type, _sa.Date):
            return _dt.date.fromisoformat(val)
        if isinstance(col_type, (_sa.Uuid, _sa.dialects.postgresql.UUID)):
            return _uuid_mod.UUID(val)
        if isinstance(col_type, _sa.Numeric):
            return Decimal(val)
    # ArithmeticError covers decimal.InvalidOperation (not a ValueError) so a
    # garbage numeric string passes through to the DB layer's real error.
    except (ValueError, AttributeError, ArithmeticError):
        pass
    return val


def _coerce_filter_value(col: Any, raw: str) -> Any:
    """Coerce a raw query-param string to the column's native Python type.

    Falls back to the original string if coercion fails, letting SQLAlchemy
    raise a meaningful error instead of silently returning no rows.
    """
    try:
        col_type = col.type
        if isinstance(col_type, (_sa.Uuid, _sa.dialects.postgresql.UUID)):
            return _uuid_mod.UUID(raw)
        if isinstance(col_type, _sa.SmallInteger):
            return int(raw)
        if isinstance(col_type, _sa.Integer):
            return int(raw)
        if isinstance(col_type, _sa.Boolean):
            return raw.lower() in ("1", "true", "yes")
        if isinstance(col_type, _sa.Numeric):
            return Decimal(raw)
    except (ValueError, AttributeError):
        pass
    return raw
