"""``secure: true`` model fields: who may read them, and what a model is shown.

The API leaves a secure field out of a response unless the caller holds ``<model>:read_secure`` (or
``iam:admin``), in the record itself and in the records an ``?include=`` embeds. Text a model reads
(a prompt, a tool's result) has the value masked unless the workflow's policy says
``allow_secure_to_llm``. The registry the API uses is keyed by model, so an unrelated field with the
same name is not hidden; data that carries no model (a prompt's inputs, a tool result) can only be masked
by name, with ``SECURE_FIELDS``, the union of the names.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

ADMIN_SCOPE = "iam:admin"


def read_secure_scope(model_name: str) -> str:
    return f"{model_name.lower()}:read_secure"


def hidden_fields(model_name: str, scopes: Collection[str]) -> frozenset[str]:
    """The secure fields of *model_name* a caller holding *scopes* may not read."""
    from tuvl.core.models.loader import SECURE_BY_MODEL

    secure = SECURE_BY_MODEL.get(model_name, frozenset())
    if not secure or ADMIN_SCOPE in scopes or read_secure_scope(model_name) in scopes:
        return frozenset()
    return secure


def needs_redaction(
    model_name: str, scopes: Collection[str], include: frozenset[str] = frozenset()
) -> bool:
    """Whether a response for *model_name* (with relations *include*) has anything to leave out."""
    from tuvl.core.models.loader import MODEL_RELATIONS

    if hidden_fields(model_name, scopes):
        return True
    return any(
        hidden_fields(rel["model"], scopes)
        for rel in MODEL_RELATIONS.get(model_name, ())
        if rel["name"] in include
    )


def strip_hidden(model_name: str, data: Any, scopes: Collection[str]) -> Any:
    """*data* (a dumped record, or a list of them) without the secure fields the caller may not read, in the
    record and in the related records embedded under its relation names."""
    from tuvl.core.models.loader import MODEL_RELATIONS

    if isinstance(data, list):
        return [strip_hidden(model_name, item, scopes) for item in data]
    if not isinstance(data, dict):
        return data
    hidden = hidden_fields(model_name, scopes)
    out = {key: value for key, value in data.items() if key not in hidden}
    for rel in MODEL_RELATIONS.get(model_name, ()):
        if out.get(rel["name"]) is not None:
            out[rel["name"]] = strip_hidden(rel["model"], out[rel["name"]], scopes)
    return out


def mask_for_model(value: Any) -> Any:
    """*value* as JSON data with every ``secure`` field's value replaced by ``*****``, for text a model will read."""
    from tuvl.core.engine.masking import mask_secure_json
    from tuvl.core.models import loader

    names = set(loader.SECURE_FIELDS)
    if not names or isinstance(value, str | int | float | bool | None):
        return value
    try:
        data = json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return value
    return mask_secure_json(data, names) if isinstance(data, dict | list) else data
