"""Loader for federation provider YAML files.

Each YAML in the project's ``federation/`` directory must have:

    kind: FederationProvider
    version: v1
    metadata:
      name: google          # matches the URL param: /auth/oauth/google/...
      description: ...      # optional
    enabled: true           # default true; set false to disable without deleting
    spec:
      provider: google      # google | github | microsoft | custom
      client_id: ${GOOGLE_CLIENT_ID}
      client_secret: ${GOOGLE_CLIENT_SECRET}
      # Optional overrides (defaults are built-in for google/github/microsoft)
      auth_url: https://...
      token_url: https://...
      userinfo_url: https://...
      scope: "openid email profile"
      # Microsoft-only
      tenant_id: common
      # Restrict login to users whose email domain is in this list.
      # Omit to allow any email address from the provider.
      # allowed_domains:
      #   - example.com
      # Role to assign automatically when a new federated user logs in for the
      # first time.  The role must already exist in the IAM database.
      # Omit to leave new users unprivileged (an admin must assign roles manually).
      # default_role: member
      # GitHub only: allow only these GitHub logins (case-insensitive). Other providers have no
      # login handle, so a file that sets it for them is refused; use allowed_domains instead.
      # allowed_users: [octocat]
      # PKCE (S256) on the authorization-code flow. On by default for google and microsoft; GitHub
      # only when set to true. Set false for a custom provider that rejects it.
      # pkce: true
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any

import yaml

from tuvl.core.logger import logger

# Registry: name → resolved YAML dict
FEDERATION_REGISTRY: dict[str, dict[str, Any]] = {}

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")

PROVIDER_TYPES = ("google", "github", "microsoft", "custom")
_TOP_FIELDS = frozenset({"kind", "version", "metadata", "enabled", "spec"})
_SPEC_FIELDS = frozenset(
    {
        "provider",
        "client_id",
        "client_secret",
        "auth_url",
        "token_url",
        "userinfo_url",
        "scope",
        "tenant_id",
        "allowed_domains",
        "allowed_users",
        "default_role",
        "pkce",
    }
)
_LOCAL_HTTP = ("http://localhost", "http://127.0.0.1", "http://[::1]")


def _is_endpoint(url: Any) -> bool:
    """https, or plain http to this machine (a provider stand-in in development)."""
    if not isinstance(url, str):
        return False
    if url.startswith("https://"):
        return True
    return any(url == base or url.startswith((base + ":", base + "/")) for base in _LOCAL_HTTP)


def provider_problems(data: Any, *, name: str | None = None, strict: bool = False) -> list[str]:
    """What is wrong with a federation provider document, one sentence each (empty: usable).

    Without *strict* only what makes a provider unusable or silently locks everyone out is checked, so a
    file that worked before keeps loading. With it (the admin API) the whole shape is checked.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["the document must be a mapping"]
    spec = data.get("spec")
    spec_map: dict[str, Any] = spec if isinstance(spec, dict) else {}
    if spec_map.get("allowed_users") is not None and spec_map.get("provider") != "github":
        problems.append(
            "spec.allowed_users works for provider: github only (other providers have no login "
            "handle, so it would refuse everyone); use allowed_domains"
        )
    if not strict:
        return problems
    if data.get("kind") != "FederationProvider":
        problems.append("kind must be FederationProvider")
    extra = sorted(set(data) - _TOP_FIELDS)
    if extra:
        problems.append(f"unknown field(s): {', '.join(extra)}")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict) or (name is not None and metadata.get("name") != name):
        problems.append(f"metadata.name must be '{name}' (the provider's name in the URL)")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        problems.append("enabled must be true or false")
    if not isinstance(spec, dict):
        problems.append("spec must be a mapping")
        return problems
    unknown = sorted(set(spec) - _SPEC_FIELDS)
    if unknown:
        problems.append(f"unknown spec field(s): {', '.join(unknown)}")
    if spec.get("provider") not in PROVIDER_TYPES:
        problems.append(f"spec.provider must be one of {', '.join(PROVIDER_TYPES)}")
    for key in ("client_id", "client_secret"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            problems.append(f"spec.{key} is required")
    for key in ("auth_url", "token_url", "userinfo_url"):
        if key in spec and not _is_endpoint(spec[key]):
            problems.append(f"spec.{key} must be an https:// address")
    for key in ("scope", "tenant_id", "default_role"):
        if key in spec and not isinstance(spec[key], str):
            problems.append(f"spec.{key} must be text")
    for key in ("allowed_domains", "allowed_users"):
        value = spec.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) for v in value)
        ):
            problems.append(f"spec.{key} must be a list of text")
    if "pkce" in spec and not isinstance(spec["pkce"], bool):
        problems.append("spec.pkce must be true or false")
    return problems


def _resolve_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match) -> str:
        var_name: str = match.group(1)
        default: str | None = match.group(2)
        if default is not None:
            return os.environ.get(var_name, default)
        val = os.environ.get(var_name)
        if val is None:
            raise OSError(
                f"Federation config references undefined environment variable "
                f"'${{{var_name}}}'. Set it in your .env file or environment."
            )
        return val

    return _ENV_PATTERN.sub(_replace, value)


def _register_federation_from_dict(data: dict[str, Any], filename: str = "<unknown>") -> None:
    """Register a single ``FederationProvider`` dict into :data:`FEDERATION_REGISTRY`."""
    if not isinstance(data, dict) or data.get("kind") != "FederationProvider":
        return

    if data.get("enabled", True) is False:
        logger.info("Skipped federation provider (enabled: false)", file=filename)
        return

    try:
        resolved = _resolve_env(data)
    except OSError as exc:
        logger.warning(
            "Skipping federation provider, env resolution failed", file=filename, error=exc
        )
        return

    problems = provider_problems(resolved)
    if problems:
        logger.error("auth.federation.provider_refused", file=filename, problems=problems)
        return

    name: str = resolved["metadata"]["name"]
    FEDERATION_REGISTRY[name] = resolved
    provider_type = resolved.get("spec", {}).get("provider", name)
    logger.info("Federation provider loaded", name=name, type=provider_type)


def load_all_federation_providers(config_dir: pathlib.Path | str | None = None) -> None:
    """Walk *config_dir* (defaults to ``<project>/federation/``) and populate
    :data:`FEDERATION_REGISTRY` from every ``kind: FederationProvider`` YAML."""

    FEDERATION_REGISTRY.clear()

    if config_dir is None:
        from tuvl.core.config import settings

        config_dir = settings.dir_for("federation")

    base_path = pathlib.Path(config_dir)
    if not base_path.exists():
        logger.debug("No federation directory found, skipping", path=str(base_path))
        return

    yaml_files = list(base_path.glob("*.yaml"))
    if not yaml_files:
        logger.debug("No YAML files in federation directory, skipping", path=str(base_path))
        return

    for yaml_file in yaml_files:
        with open(yaml_file) as f:
            try:
                raw = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                logger.warning(
                    "Skipping federation YAML, parse error", file=yaml_file.name, error=exc
                )
                continue

        _register_federation_from_dict(raw, yaml_file.name)

    if FEDERATION_REGISTRY:
        logger.info("Federation registry loaded", providers=list(FEDERATION_REGISTRY.keys()))
