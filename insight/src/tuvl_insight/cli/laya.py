"""Laya decision model scaffolding shared by `tuvl init` and `tuvl ship`.

Laya runs as its own container (the laya-serve image); tuvl reaches it over
``POST /v1/systemone`` through a ``provider: laya`` AgentModel.
"""

from __future__ import annotations

from pathlib import Path

import yaml

LAYA_IMAGE_REPOSITORY = "docker.io/tuvl/laya-serve"
#: english + multilingual: what ``model: auto`` and ``multilingual`` need
LAYA_IMAGE_TAG = "0.4.7-1"
#: english only: about 0.8 GB smaller and half the memory; cannot serve non-English text
LAYA_IMAGE_TAG_EN = "0.4.7-1-en"
LAYA_LOCAL_URL = "http://localhost:8000"

AGENT_MODEL_YAML = f"""\
# Laya — a typed-decision model for `decide` agents (decide.model.ref: laya).
# It runs as its own container: `docker compose up -d laya` locally; `tuvl ship`
# adds it to the Helm chart and points LAYA_API_BASE at it.
kind: AgentModel
version: v1
metadata:
  name: laya
  description: Self-hosted Laya decision model (laya-serve)
spec:
  type: decision
  provider: laya
  model: english                   # english | auto | multilingual
                                   # `english` runs the smaller -en image; `auto` and
                                   # `multilingual` need the full image (see compose.yaml)
  api_base: ${{LAYA_API_BASE:{LAYA_LOCAL_URL}}}
  api_key: ${{LAYA_API_KEY:}}        # optional; set the same key on the server
"""

COMPOSE_YAML = f"""\
# Local services for development — `docker compose up -d`.
services:
  laya:
    # English only. For `model: auto` or `multilingual`, use {LAYA_IMAGE_REPOSITORY}:{LAYA_IMAGE_TAG}
    image: ${{LAYA_IMAGE:-{LAYA_IMAGE_REPOSITORY}:{LAYA_IMAGE_TAG_EN}}}
    ports: ["127.0.0.1:8000:8000"]
    environment:
      LAYA_API_KEY: ${{LAYA_API_KEY:-}}
    restart: unless-stopped
"""


def laya_models(project_dir: Path) -> list[str]:
    """The ``model`` of every AgentModel under llms/ that declares ``provider: laya``."""
    models: list[str] = []
    for path in sorted((project_dir / "llms").glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and doc.get("kind") == "AgentModel":
            spec = doc.get("spec")
            if isinstance(spec, dict) and spec.get("provider") == "laya":
                models.append(str(spec.get("model") or "auto"))
    return models


def uses_laya(project_dir: Path) -> bool:
    """True when an AgentModel under llms/ declares ``provider: laya``."""
    return bool(laya_models(project_dir))


def english_only(project_dir: Path) -> bool:
    """True when every Laya model is pinned to ``english``, so the -en image is enough.

    A project with no Laya model (``--with-laya`` on a bare project) gets the full image: it
    cannot be known to stay English.
    """
    models = laya_models(project_dir)
    return bool(models) and all(m == "english" for m in models)


def image_tag(project_dir: Path) -> str:
    """The laya-serve tag a project needs: ``-en`` only when every Laya model is ``english``."""
    return LAYA_IMAGE_TAG_EN if english_only(project_dir) else LAYA_IMAGE_TAG
