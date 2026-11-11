"""Listing a project in a Nest: the one flow the CLI and Insight share.

Register (open, no sign-in), then, when the Nest issued a challenge, push its one-time tag to the project's
git remote with the developer's own credentials and tell the Nest what happened. The Nest looks for the tag
itself on a public remote (``verified``); for a private one it can only take this tool's report
(``push_confirmed``). Developer toolchain only: nothing here is in the production engine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tuvl.core.project_info import (
    record_registration,
    record_verification,
    registration_payload,
)
from tuvl_insight import gitutil
from tuvl_insight.cli import nest as nestlib


@dataclass
class Plan:
    """What verification would do, shown before anything is sent."""

    remote: str
    remote_url: str
    repo: str
    matches_repo: bool
    reason: str  # why verification cannot run, "" when it can

    def as_dict(self) -> dict[str, Any]:
        return {
            "remote": self.remote,
            "remote_url": self.remote_url,
            "repo": self.repo,
            "matches_repo": self.matches_repo,
            "can_verify": not self.reason,
            "reason": self.reason,
        }


@dataclass
class Outcome:
    label: str
    claim: bool = False
    applied: bool = False
    pushed: bool | None = None  # None: no push was attempted
    notes: list[str] = field(default_factory=list)


def plan(project_dir: Path, record: dict[str, Any], remote: str = "origin") -> Plan:
    repo = str(record.get("repo", "") or "")
    url = gitutil.remote_url(project_dir, remote) if gitutil.available() else ""
    matches = bool(url) and gitutil.same_repo(url, repo)
    reason = ""
    if not repo:
        reason = "The project has no repository URL (tuvl set project repo <url>), so there is nothing to verify."
    elif not repo.startswith("https://"):
        reason = "Only an https repository URL can be verified."
    elif not gitutil.available():
        reason = "git is not installed."
    elif not url:
        reason = f"This directory has no git remote named '{remote}'."
    elif not matches:
        reason = f"The remote '{remote}' ({url}) is not the project's repository ({repo})."
    return Plan(remote=remote, remote_url=url, repo=repo, matches_repo=matches, reason=reason)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def register_project(
    project_dir: Path,
    record: dict[str, Any],
    url: str,
    *,
    tuvl_version: str = "",
    verify: bool = True,
    remote: str = "origin",
    keep_tag: bool = False,
    confirm_push: Callable[[str, str, str], bool] | None = None,
) -> Outcome:
    """Register, then (when asked and possible) push the challenge tag and report it.

    *confirm_push(remote, remote_url, tag)* is asked before the push; ``None`` means the caller already
    has the developer's consent. Raises :class:`NestError` when the Nest refuses or is unreachable.
    """
    payload = registration_payload(record, tuvl_version=tuvl_version)
    project_id = payload["id"]
    reg = nestlib.register(url, payload, secret=nestlib.entry_secret(url, project_id))
    if reg.entry_secret:
        nestlib.store_entry_secret(url, project_id, reg.entry_secret)
    out = Outcome(label=reg.label, claim=reg.claim)
    if reg.claim:
        out.notes.append(
            "This id is already listed and this registration did not carry its entry secret, so it is a claim: "
            "it changes the listing only if the registry itself sees the one-time tag on your public repository."
        )
    else:
        record_registration(project_dir, url, when=_now(), label=reg.label)

    if reg.challenge is None:
        out.notes.append("The Nest issued no challenge, so the listing stays unverified.")
        return out
    if not verify:
        out.notes.append("Verification skipped (--no-verify); the listing stays as it is.")
        return out
    found = plan(project_dir, record, remote)
    if found.reason:
        out.notes.append(f"Not verified: {found.reason}")
        return out
    tag = reg.challenge.tag
    if confirm_push is not None and not confirm_push(remote, found.remote_url, tag):
        out.notes.append("Not verified: the tag was not pushed.")
        return out

    ok, detail = gitutil.push_tag(project_dir, remote, tag)
    out.pushed = ok
    if not ok:
        out.notes.append(f"Could not push the tag: {detail}. The listing stays unverified.")
        return out
    try:
        answer = nestlib.verify(url, project_id, reg.challenge.token, pushed=True)
    finally:
        if not keep_tag:
            gitutil.delete_tag(project_dir, remote, tag)
    out.label, out.applied = answer.label, answer.applied
    if answer.entry_secret:
        nestlib.store_entry_secret(url, project_id, answer.entry_secret)
    if reg.claim:
        if answer.applied:
            record_registration(
                project_dir, url, when=_now(), label=answer.label, verified_at=_now()
            )
        else:
            out.notes.append(
                "The claim was not applied: the registry could not see the tag, so it cannot take the listing over."
            )
    elif answer.applied:
        record_verification(
            project_dir, answer.label, verified_at=_now() if answer.label == "verified" else ""
        )
    return out
