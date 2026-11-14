"""``code.with`` reads the environment: ``${VAR}`` is expanded in the document's own text, never in data."""

from __future__ import annotations

import pytest

from tuvl.contract.tel import compile_template
from tuvl.runtime.engines import expand_env_params, render_params


def test_with_expands_env_with_and_without_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILBOX_TOKEN", "t0k")
    monkeypatch.delenv("MAILBOX_URL", raising=False)
    got = expand_env_params(
        {
            "token": "${MAILBOX_TOKEN}",
            "url": "${MAILBOX_URL:https://x.test}",
            "nested": ["a-${MAILBOX_TOKEN:}", 3],
        }
    )
    assert got == {"token": "t0k", "url": "https://x.test", "nested": ["a-t0k", 3]}


def test_an_unset_variable_with_an_empty_default_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILBOX_UNSET", raising=False)
    assert expand_env_params({"v": "${MAILBOX_UNSET:}"}) == {"v": ""}


def test_an_unset_variable_with_no_default_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILBOX_UNSET", raising=False)
    with pytest.raises(Exception, match="MAILBOX_UNSET"):
        expand_env_params({"v": "${MAILBOX_UNSET}"})


def test_data_that_looks_like_an_env_reference_is_not_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILBOX_TOKEN", "secret")
    rendered = render_params(
        expand_env_params({"note": "{{ text }}"}), {"text": "${MAILBOX_TOKEN}"}
    )
    assert rendered == {"note": "${MAILBOX_TOKEN}"}
    assert compile_template("{{ text }}")  # the template engine is untouched
