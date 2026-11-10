"""Coding agents installed on this machine, and the developer's choice among them."""

from tuvl_insight.coding_agents import runner
from tuvl_insight.coding_agents.detect import Detected, detect
from tuvl_insight.coding_agents.registry import AGENTS, VERIFIED_ON, AgentSpec, get
from tuvl_insight.coding_agents.settings import (
    Settings,
    grant_consent,
    initials_from,
    read_settings,
    valid_initials,
    write_settings,
)

__all__ = [
    "AGENTS",
    "VERIFIED_ON",
    "AgentSpec",
    "Detected",
    "Settings",
    "detect",
    "get",
    "runner",
    "grant_consent",
    "initials_from",
    "read_settings",
    "valid_initials",
    "write_settings",
]
