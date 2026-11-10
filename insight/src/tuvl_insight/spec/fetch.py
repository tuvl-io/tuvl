"""Fetch a URL into idea files: a web page as Markdown, or a Jira, Confluence or GitHub item through its API.

The result is a snapshot, written to a file that ``import_idea`` copies like any other, so an idea stays
immutable: a changed ticket is a new idea that supersedes the old one. Requests go through the webhook
address guard (no internal addresses unless asked, the connection pinned to the address that was
checked, redirects followed by hand and checked at every hop). Credentials come from the environment, are
sent only to the service they belong to, and are never written to the idea.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as _dt
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from tuvl.webhooks.guard import BlockedAddress, resolve, url_problem
from tuvl_insight.spec.html_md import html_to_markdown

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_REDIRECTS = 5
MAX_COMMENTS = 100
USER_AGENT = "tuvl-import/1"
_SECRET_QUERY = re.compile(r"token|key|secret|sig|auth|password|pass|code|credential", re.I)
_REDIRECTS = {301, 302, 303, 307, 308}
_JIRA_KEY = r"[A-Z][A-Z0-9_]+-\d+"

ATLASSIAN_VARS = ("TUVL_ATLASSIAN_EMAIL", "TUVL_ATLASSIAN_TOKEN", "TUVL_ATLASSIAN_PAT")


class FetchError(ValueError):
    pass


@dataclass
class Fetched:
    name: str
    data: bytes
    title: str
    origin: dict[str, Any]


@dataclass
class _Response:
    status: int
    headers: httpx.Headers
    body: bytes
    url: str


def safe_url(url: str) -> str:
    """The URL as it is recorded: no user info or fragment, and no secret in the query."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    query = urlencode(
        [
            (k, "…" if _SECRET_QUERY.search(k) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def _slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "page"


def _hint(problem: str) -> str:
    if "internal" in problem:
        return " (--allow-private if this is your own server)"
    if "must be https" in problem:
        return " (--allow-private also allows http)"
    return ""


async def _request(
    url: str,
    *,
    headers: Mapping[str, str],
    allow_private: bool,
    max_bytes: int,
    follow: bool,
) -> _Response:
    """One GET, pinned to a checked address. Redirects are followed only when asked, and never carry *headers* away."""
    current, sent = url, dict(headers)
    for _ in range(MAX_REDIRECTS + 1):
        problem = url_problem(current, allow_private=allow_private)
        if problem:
            raise FetchError(f"{safe_url(current)}: {problem}{_hint(problem)}")
        parts = urlsplit(current)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await resolve(host, port, allow_private=allow_private)
        except BlockedAddress:
            raise FetchError(
                f"{safe_url(current)}: the host name resolves to an internal address{_hint('internal')}"
            ) from None
        except OSError:
            raise FetchError(f"{safe_url(current)}: the host name did not resolve") from None
        target = addresses[0]
        netloc = f"[{target}]" if ":" in target else target
        pinned = urlunsplit((parts.scheme, f"{netloc}:{port}", parts.path or "/", parts.query, ""))
        request_headers = {
            "User-Agent": USER_AGENT,
            "Host": parts.netloc.rpartition("@")[2],
            **sent,
        }
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        try:
            async with (
                httpx.AsyncClient(
                    timeout=TIMEOUT, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    "GET", pinned, headers=request_headers, extensions=extensions
                ) as response,
            ):
                if response.status_code in _REDIRECTS and follow:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"{safe_url(current)}: a redirect without a target")
                    current = urljoin(current, location)
                    sent = {k: v for k, v in sent.items() if k.lower() != "authorization"}
                    continue
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise FetchError(
                            f"{safe_url(current)}: larger than {max_bytes // (1024 * 1024)} MB"
                        )
                return _Response(response.status_code, response.headers, bytes(body), current)
        except httpx.TimeoutException:
            raise FetchError(f"{safe_url(current)}: timed out") from None
        except httpx.HTTPError as exc:
            raise FetchError(
                f"{safe_url(current)}: connection failed ({type(exc).__name__})"
            ) from None
    raise FetchError(f"{safe_url(url)}: too many redirects")


def _status_error(response: _Response, url: str, auth_hint: str = "") -> FetchError:
    code = response.status
    if code in (401, 403):
        why = "it asked for a login" + (
            f"; {auth_hint}" if auth_hint else ": save the page and import the file"
        )
    elif code == 404:
        why = "not found" + (f" (or not visible to this account; {auth_hint})" if auth_hint else "")
    elif code == 429:
        why = "rate limited, try again later"
    else:
        why = f"HTTP {code}"
    return FetchError(f"{safe_url(url)}: {why}")


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    for encoding in (match.group(1) if match else "utf-8", "utf-8"):
        with contextlib.suppress(LookupError, UnicodeDecodeError):
            return body.decode(encoding)
    return body.decode("utf-8", errors="replace")


def _json(response: _Response, url: str) -> Any:
    try:
        return json.loads(response.body)
    except ValueError:
        raise FetchError(f"{safe_url(url)}: the service did not answer with JSON") from None


def _md(html: str | None) -> str:
    return html_to_markdown(html, keep_chrome=True)[0].strip() if html else ""


def _line(label: str, value: Any) -> str | None:
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value or "").strip()
    return f"- **{label}:** {text}" if text else None


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or value.get("login") or "")
    return str(value or "")


# ── Jira ─────────────────────────────────────────────────────────────────────


def _atlassian_headers(host: str, env: Mapping[str, str]) -> tuple[dict[str, str], str]:
    """Credentials for *host*, only when the host is Atlassian Cloud or one the user listed."""
    trusted = {
        h.strip().lower() for h in env.get("TUVL_ATLASSIAN_HOSTS", "").split(",") if h.strip()
    }
    if not (host.lower().endswith(".atlassian.net") or host.lower() in trusted):
        return {}, (
            f"no credentials are sent to {host}: list it in TUVL_ATLASSIAN_HOSTS to send them"
        )
    email, token, pat = (env.get(v, "") for v in ATLASSIAN_VARS)
    if email and token:
        raw = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {"Authorization": f"Basic {raw}"}, ""
    if pat:
        return {"Authorization": f"Bearer {pat}"}, ""
    return {}, (
        "set TUVL_ATLASSIAN_EMAIL and TUVL_ATLASSIAN_TOKEN (Cloud), or TUVL_ATLASSIAN_PAT (Server / Data Center)"
    )


_JIRA_FIELDS = (
    "summary,description,issuetype,status,priority,resolution,assignee,reporter,labels,components,"
    "fixVersions,created,updated,duedate,parent,issuelinks,subtasks,attachment,comment"
)


def _jira_match(parts: Any) -> tuple[str, str] | None:
    found = re.search(rf"^(.*?)/browse/({_JIRA_KEY})(?:/|$)", parts.path)
    if found:
        return found.group(1), found.group(2)
    selected = dict(parse_qsl(parts.query)).get("selectedIssue", "")
    if re.fullmatch(_JIRA_KEY, selected) and "/jira/" in parts.path:
        return "", selected
    return None


def _render_jira(key: str, issue: dict[str, Any], base_url: str) -> str:
    f = issue.get("fields") or {}
    rendered = issue.get("renderedFields") or {}
    out = [f"# {key}: {f.get('summary', '')}".rstrip(), ""]
    meta = [
        _line("Type", _name(f.get("issuetype"))),
        _line("Status", _name(f.get("status"))),
        _line("Resolution", _name(f.get("resolution"))),
        _line("Priority", _name(f.get("priority"))),
        _line("Assignee", _name(f.get("assignee"))),
        _line("Reporter", _name(f.get("reporter"))),
        _line("Labels", f.get("labels") or []),
        _line("Components", [_name(c) for c in f.get("components") or []]),
        _line("Fix versions", [_name(v) for v in f.get("fixVersions") or []]),
        _line("Parent", _issue_ref(f.get("parent"))),
        _line("Due", f.get("duedate")),
        _line("Created", f.get("created")),
        _line("Updated", f.get("updated")),
        _line("Source", f"{base_url}/browse/{key}"),
    ]
    out += [m for m in meta if m]
    description = _md(rendered.get("description")) or str(f.get("description") or "").strip()
    if description:
        out += ["", "## Description", "", description]
    subtasks = [_issue_ref(s) for s in f.get("subtasks") or []]
    if subtasks:
        out += ["", "## Subtasks", ""] + [f"- {s}" for s in subtasks if s]
    links = []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        kind = link.get("type") or {}
        verb = kind.get("outward") if link.get("outwardIssue") else kind.get("inward")
        if other:
            links.append(f"- {verb or kind.get('name', 'relates to')} {_issue_ref(other)}")
    if links:
        out += ["", "## Linked issues", ""] + links
    attachments = [a.get("filename") for a in f.get("attachment") or [] if a.get("filename")]
    if attachments:
        out += ["", "## Attachments (not fetched)", ""] + [f"- {a}" for a in attachments]
    comments = (f.get("comment") or {}).get("comments") or []
    rendered_comments = (rendered.get("comment") or {}).get("comments") or []
    if comments:
        out += ["", "## Comments"]
        for n, c in enumerate(comments[:MAX_COMMENTS]):
            html = rendered_comments[n].get("body") if n < len(rendered_comments) else None
            body = _md(html) or str(c.get("body") or "").strip()
            out += ["", f"### {_name(c.get('author'))}, {c.get('created', '')}", "", body]
    return "\n".join(out).rstrip() + "\n"


def _issue_ref(issue: Any) -> str:
    if not isinstance(issue, dict):
        return ""
    summary = (issue.get("fields") or {}).get("summary", "")
    return f"{issue.get('key', '')} {summary}".strip()


async def _jira(url: str, parts: Any, base: str, key: str, opts: _Options) -> Fetched:
    origin = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{base}"
    headers, hint = _atlassian_headers(parts.hostname or "", opts.env)
    query = urlencode({"fields": _JIRA_FIELDS, "expand": "renderedFields"})
    api = f"{origin}/rest/api/2/issue/{quote(key)}?{query}"
    response = await _request(
        api, headers={**headers, "Accept": "application/json"}, follow=False, **opts.request
    )
    if response.status != 200:
        raise _status_error(response, url, hint)
    text = _render_jira(key, _json(response, url), origin)
    return Fetched(
        f"jira-{key}.md", text.encode(), text.splitlines()[0].lstrip("# "), _origin(url, "jira")
    )


# ── Confluence ───────────────────────────────────────────────────────────────


def _confluence_match(parts: Any) -> tuple[str, str] | None:
    found = re.search(r"^(.*?)/spaces/[^/]+/pages/(\d+)", parts.path)
    if found:
        return found.group(1), found.group(2)
    page = dict(parse_qsl(parts.query)).get("pageId", "")
    if parts.path.endswith("/pages/viewpage.action") and page.isdigit():
        return parts.path[: -len("/pages/viewpage.action")], page
    return None


async def _confluence(url: str, parts: Any, base: str, page_id: str, opts: _Options) -> Fetched:
    origin = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{base}"
    headers, hint = _atlassian_headers(parts.hostname or "", opts.env)
    query = urlencode({"expand": "body.view,version,space"})
    response = await _request(
        f"{origin}/rest/api/content/{page_id}?{query}",
        headers={**headers, "Accept": "application/json"},
        follow=False,
        **opts.request,
    )
    if response.status != 200:
        raise _status_error(response, url, hint)
    page = _json(response, url)
    title = str(page.get("title") or f"Confluence page {page_id}")
    version = page.get("version") or {}
    lines = [
        f"# {title}",
        "",
        *(
            m
            for m in (
                _line("Space", (page.get("space") or {}).get("name")),
                _line("Last updated by", _name(version.get("by"))),
                _line("Last updated", version.get("when")),
                _line("Source", safe_url(url)),
            )
            if m
        ),
        "",
        _md(((page.get("body") or {}).get("view") or {}).get("value")),
    ]
    text = "\n".join(lines).rstrip() + "\n"
    return Fetched(
        f"confluence-{page_id}-{_slug(title, 40)}.md",
        text.encode(),
        title,
        _origin(url, "confluence"),
    )


# ── GitHub ───────────────────────────────────────────────────────────────────


def _github_match(parts: Any) -> tuple[str, str, str] | None:
    if (parts.hostname or "").lower() != "github.com":
        return None
    found = re.match(r"^/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)(?:/|$)", parts.path)
    return (found.group(1), found.group(2), found.group(3)) if found else None


async def _github(url: str, owner: str, repo: str, number: str, opts: _Options) -> Fetched:
    env = opts.env
    token = env.get("TUVL_GITHUB_TOKEN") or env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or ""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }
    hint = "set TUVL_GITHUB_TOKEN (or GITHUB_TOKEN) for a private repository"
    base = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/issues/{number}"
    first = await _request(base, headers=headers, follow=False, **opts.request)
    if first.status != 200:
        raise _status_error(first, url, hint)
    issue = _json(first, url)
    comments: list[dict[str, Any]] = []
    if issue.get("comments"):
        more = await _request(
            f"{base}/comments?per_page={MAX_COMMENTS}",
            headers=headers,
            follow=False,
            **opts.request,
        )
        if more.status == 200:
            comments = _json(more, url)
    kind = "Pull request" if issue.get("pull_request") else "Issue"
    title = str(issue.get("title") or "")
    lines = [
        f"# {owner}/{repo}#{number}: {title}",
        "",
        *(
            m
            for m in (
                _line("Kind", kind),
                _line("State", issue.get("state")),
                _line("Author", _name(issue.get("user"))),
                _line("Labels", [_name(label) for label in issue.get("labels") or []]),
                _line("Assignees", [_name(a) for a in issue.get("assignees") or []]),
                _line("Milestone", (issue.get("milestone") or {}).get("title")),
                _line("Created", issue.get("created_at")),
                _line("Updated", issue.get("updated_at")),
                _line("Source", f"https://github.com/{owner}/{repo}/issues/{number}"),
            )
            if m
        ),
    ]
    if (issue.get("body") or "").strip():
        lines += ["", "## Description", "", str(issue["body"]).strip()]
    if comments:
        lines += ["", "## Comments"]
        for c in comments[:MAX_COMMENTS]:
            lines += [
                "",
                f"### {_name(c.get('user'))}, {c.get('created_at', '')}",
                "",
                str(c.get("body") or "").strip(),
            ]
    text = "\n".join(lines).rstrip() + "\n"
    return Fetched(
        f"github-{_slug(owner)}-{_slug(repo)}-{number}.md",
        text.encode(),
        f"{owner}/{repo}#{number}: {title}",
        _origin(url, "github"),
    )


# ── any other page ───────────────────────────────────────────────────────────


async def _page(url: str, opts: _Options) -> Fetched:
    response = await _request(
        url,
        headers={"Accept": "text/html,text/markdown,text/plain,application/pdf;q=0.9,*/*;q=0.1"},
        follow=True,
        **opts.request,
    )
    if response.status != 200:
        raise _status_error(response, url)
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    final = urlsplit(response.url)
    stem = _slug(f"{final.hostname}-{final.path}")
    origin = _origin(response.url, "web")
    if ctype in ("text/html", "application/xhtml+xml", ""):
        markdown, title = html_to_markdown(
            _decode(response.body, response.headers.get("content-type", ""))
        )
        if len(markdown.split()) < 3:
            raise FetchError(
                f"{safe_url(url)}: no readable text. It is probably built with JavaScript or needs a login: "
                "save it as a PDF or copy the text, and import that"
            )
        if not markdown.startswith("# "):
            markdown = f"# {title or final.hostname}\n\n{markdown}"
        return Fetched(f"web-{stem}.md", markdown.encode(), title or str(final.hostname), origin)
    if ctype == "application/pdf":
        return Fetched(f"web-{stem}.pdf", response.body, stem, origin)
    suffix = {
        "text/markdown": ".md", "text/x-markdown": ".md", "text/plain": ".txt",
        "text/csv": ".csv", "application/json": ".json",
    }.get(ctype)  # fmt: skip
    if suffix is None:
        raise FetchError(
            f"{safe_url(url)}: a {ctype} page cannot be an idea (web pages, text, JSON, CSV and PDF can)"
        )
    text = _decode(response.body, response.headers.get("content-type", ""))
    return Fetched(f"web-{stem}{suffix}", text.encode(), stem, origin)


# ── entry points ─────────────────────────────────────────────────────────────


@dataclass
class _Options:
    env: Mapping[str, str]
    request: dict[str, Any]


def _origin(url: str, kind: str) -> dict[str, Any]:
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"url": safe_url(url), "kind": kind, "fetched_at": stamp}


async def fetch(
    url: str,
    *,
    allow_private: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    env: Mapping[str, str] | None = None,
) -> Fetched:
    """Fetch one URL as an idea file, through the connector that knows it or as a plain page."""
    problem = url_problem(url, allow_private=True)
    if problem:
        raise FetchError(f"{safe_url(url)}: {problem}")
    opts = _Options(
        os.environ if env is None else env,
        {"allow_private": allow_private, "max_bytes": max_bytes},
    )
    parts = urlsplit(url)
    if jira := _jira_match(parts):
        return await _jira(url, parts, *jira, opts)
    if confluence := _confluence_match(parts):
        return await _confluence(url, parts, *confluence, opts)
    if github := _github_match(parts):
        return await _github(url, *github, opts)
    return await _page(url, opts)


def fetch_into(
    urls: list[str],
    dest: Path,
    *,
    allow_private: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Fetched]:
    """Fetch *urls* in order and write each snapshot into *dest*, giving repeated names a number."""
    results: list[Fetched] = []

    async def run() -> None:
        for url in urls:
            results.append(
                await fetch(url.strip(), allow_private=allow_private, max_bytes=max_bytes)
            )

    asyncio.run(run())
    taken: set[str] = set()
    for item in results:
        stem, dot, suffix = item.name.rpartition(".")
        name, n = item.name, 1
        while name in taken:
            n += 1
            name = f"{stem}-{n}{dot}{suffix}"
        taken.add(name)
        item.name = name
        (dest / name).write_bytes(item.data)
    return results
