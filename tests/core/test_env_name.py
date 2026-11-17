"""``TUVL_ENV`` is compared in one place, case-insensitively."""

from __future__ import annotations

import pytest

from tuvl.core.config import Settings


@pytest.mark.parametrize(
    ("value", "production"),
    [
        ("production", True),
        ("Production", True),
        (" PRODUCTION ", True),
        ("development", False),
        ("Development", False),
        ("prod", False),
        ("", False),
    ],
)
def test_is_production_ignores_case_and_padding(value: str, production: bool) -> None:
    assert Settings(tuvl_env=value).is_production is production


@pytest.mark.parametrize(
    ("value", "known"),
    [
        ("production", True),
        ("Development", True),
        ("test", True),
        ("staging", True),
        ("prod", False),
    ],
)
def test_an_unknown_name_is_reported(value: str, known: bool) -> None:
    assert Settings(tuvl_env=value).env_is_known is known
