"""Regression tests for the environment dict ``tuvl dev`` constructs.

These pin two contracts that, when broken, blow up at user-visible
boot time rather than at test time:

1. ``tuvl dev`` MUST set ``TUVL_ENV=development`` (via ``setdefault`` so
   an explicit external override still wins).  The auth layer-1
   sentinel hard-fails any process with ``TUVL_DEV_MODE=true`` while
   ``TUVL_ENV=production`` (the engine default) — without the
   setdefault, the dev command refuses to boot at all.  Surfaced as a
   regression in 2026.2.3b1; fixed in 2026.2.3b2.

2. ``tuvl dev`` MUST also set ``TUVL_DEV_MODE=true`` and
   ``TUVL_ALLOW_DEV_AUTH=true`` (otherwise the dev-key superuser shortcut
   is inert and Insight's login screen won't accept the dev key).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_dev_env_sets_tuvl_env_development(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_build_env`` must declare a dev environment to avoid the
    layer-1 production sentinel in tuvl.core.auth.access."""
    monkeypatch.delenv("TUVL_ENV", raising=False)

    from tuvl_insight.cli.commands.dev import _build_env

    env = _build_env(
        key="DEV-KEY",
        port=8000,
        project_dir=Path("/tmp/fake-project"),
        allow_hosts=[],
        auto_login=False,
    )
    assert env.get("TUVL_ENV") == "development", (
        "`tuvl dev` must set TUVL_ENV=development so the auth "
        "layer-1 production sentinel does not refuse to boot."
    )


def test_dev_env_respects_external_tuvl_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``TUVL_ENV=production tuvl dev`` (e.g. a security-
    testing context) must NOT be silently rewritten — the user opted
    into the strict sentinel and the dev command should let the sentinel
    fire."""
    monkeypatch.setenv("TUVL_ENV", "production")

    from tuvl_insight.cli.commands.dev import _build_env

    env = _build_env(
        key="DEV-KEY",
        port=8000,
        project_dir=Path("/tmp/fake-project"),
        allow_hosts=[],
        auto_login=False,
    )
    assert env.get("TUVL_ENV") == "production", (
        "External TUVL_ENV must override the dev default — setdefault "
        "must not clobber the user's explicit choice."
    )


def test_dev_env_sets_dev_mode_and_ack_flag() -> None:
    """The dev-key superuser shortcut requires both ``TUVL_DEV_MODE=true``
    and ``TUVL_ALLOW_DEV_AUTH=true``; ``tuvl dev`` MUST set both."""
    from tuvl_insight.cli.commands.dev import _build_env

    env = _build_env(
        key="DEV-KEY",
        port=8000,
        project_dir=Path("/tmp/fake-project"),
        allow_hosts=[],
        auto_login=False,
    )
    assert env.get("TUVL_DEV_MODE") == "true"
    assert env.get("TUVL_ALLOW_DEV_AUTH") == "true"
    assert env.get("TUVL_DEV_API_KEY") == "DEV-KEY"
    assert env.get("TUVL_DEV_PORT") == "8000"
