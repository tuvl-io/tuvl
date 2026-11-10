"""Idea snapshots from URLs: pages, Jira, Confluence and GitHub, through the address guard."""

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tuvl.webhooks.guard import BlockedAddress
from tuvl_insight.spec import fetch as fetch_mod
from tuvl_insight.spec.fetch import FetchError, fetch, fetch_into, safe_url

PUBLIC = "93.184.216.34"
Route = Callable[[httpx.Request], httpx.Response] | httpx.Response


class Wire:
    """Stands in for the network: records each request and answers from a table keyed by host and path."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Route] = {}
        self.internal: set[str] = set()

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        route = self.routes.get((request.headers["host"], request.url.path))
        if route is None:
            return httpx.Response(404)
        return route(request) if callable(route) else route


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    w = Wire()

    async def resolve(host: str, port: int, *, allow_private: bool = False) -> list[str]:
        if host in w.internal and not allow_private:
            raise BlockedAddress("the host resolves to an internal address")
        return [PUBLIC]

    real = httpx.AsyncClient

    def client(**kw: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(w.answer), **kw)

    monkeypatch.setattr(fetch_mod, "resolve", resolve)
    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", client)
    return w


def html(body: str, title: str = "Refund policy") -> httpx.Response:
    page = f"<html><head><title>{title}</title></head><body>{body}</body></html>"
    return httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"}, content=page.encode()
    )


async def test_a_web_page_becomes_a_markdown_snapshot_pinned_to_the_checked_address(
    wire: Wire,
) -> None:
    wire.routes[("docs.example.com", "/refunds")] = html(
        "<main><h1>Refund policy</h1><p>Refund within fourteen days.</p></main>"
    )
    got = await fetch("https://docs.example.com/refunds?utm_token=abc", env={})
    assert got.name == "web-docs-example-com-refunds.md"
    assert got.data.decode() == "# Refund policy\n\nRefund within fourteen days.\n"
    assert got.origin["kind"] == "web"
    assert got.origin["url"] == "https://docs.example.com/refunds?utm_token=%E2%80%A6"
    sent = wire.seen[0]
    assert sent.url.host == PUBLIC and sent.headers["host"] == "docs.example.com"
    assert sent.extensions["sni_hostname"] == "docs.example.com"


async def test_a_page_without_a_heading_gets_its_title(wire: Wire) -> None:
    wire.routes[("a.test", "/x")] = html("<p>Just some words here.</p>", title="Brief")
    got = await fetch("https://a.test/x", env={})
    assert got.data.decode().startswith("# Brief\n\nJust some words")


async def test_credentials_never_go_to_an_ordinary_page(wire: Wire) -> None:
    wire.routes[("a.test", "/x")] = html("<p>Some words here ok.</p>")
    env = {"TUVL_ATLASSIAN_PAT": "p", "GITHUB_TOKEN": "g", "TUVL_GITHUB_TOKEN": "g"}
    await fetch("https://a.test/x", env=env)
    assert "authorization" not in wire.seen[0].headers


async def test_an_internal_name_is_refused_unless_allowed(wire: Wire) -> None:
    with pytest.raises(FetchError, match="--allow-private"):
        await fetch("https://localhost/x", env={})
    with pytest.raises(FetchError, match="--allow-private"):
        await fetch("http://10.0.0.5/x", env={})
    with pytest.raises(FetchError, match="credentials"):
        await fetch("https://user:pw@a.test/x", env={})
    wire.internal.add("wiki.example.com")
    with pytest.raises(FetchError, match="internal address"):
        await fetch("https://wiki.example.com/x", env={})
    wire.routes[("wiki.example.com", "/x")] = html("<p>Some words here ok.</p>")
    assert (await fetch("https://wiki.example.com/x", allow_private=True, env={})).data


async def test_a_redirect_to_an_internal_address_is_refused_at_that_hop(wire: Wire) -> None:
    wire.internal.add("inside.example.com")
    wire.routes[("a.test", "/x")] = httpx.Response(
        302, headers={"location": "https://inside.example.com/secret"}
    )
    with pytest.raises(FetchError, match="internal address"):
        await fetch("https://a.test/x", env={})
    assert len(wire.seen) == 1


async def test_redirects_are_followed_but_not_forever(wire: Wire) -> None:
    wire.routes[("a.test", "/old")] = httpx.Response(301, headers={"location": "/new"})
    wire.routes[("a.test", "/new")] = html("<p>Moved here for good.</p>")
    assert b"Moved here" in (await fetch("https://a.test/old", env={})).data
    wire.routes[("a.test", "/loop")] = httpx.Response(302, headers={"location": "/loop"})
    with pytest.raises(FetchError, match="too many redirects"):
        await fetch("https://a.test/loop", env={})


async def test_pages_that_cannot_be_read_say_why(wire: Wire) -> None:
    wire.routes[("a.test", "/spa")] = html("<div id=root></div><script>render()</script>")
    with pytest.raises(FetchError, match="no readable text"):
        await fetch("https://a.test/spa", env={})
    wire.routes[("a.test", "/login")] = httpx.Response(403)
    with pytest.raises(FetchError, match="asked for a login"):
        await fetch("https://a.test/login", env={})
    wire.routes[("a.test", "/img")] = httpx.Response(
        200, headers={"content-type": "image/png"}, content=b"\x89PNG"
    )
    with pytest.raises(FetchError, match="image/png"):
        await fetch("https://a.test/img", env={})
    wire.routes[("a.test", "/big")] = httpx.Response(
        200, headers={"content-type": "text/plain"}, content=b"x" * 2048
    )
    with pytest.raises(FetchError, match="larger than"):
        await fetch("https://a.test/big", max_bytes=1024, env={})


async def test_text_json_and_pdf_are_kept_as_they_are(wire: Wire) -> None:
    for path, ctype, body in (
        ("/n.md", "text/markdown", b"# Notes\n"),
        ("/d.json", "application/json", b"{}"),
        ("/b.pdf", "application/pdf", b"%PDF-1.4"),
    ):
        wire.routes[("a.test", path)] = httpx.Response(
            200, headers={"content-type": ctype}, content=body
        )
    assert (await fetch("https://a.test/n.md", env={})).name.endswith(".md")
    assert (await fetch("https://a.test/d.json", env={})).name.endswith(".json")
    pdf = await fetch("https://a.test/b.pdf", env={})
    assert pdf.name.endswith(".pdf") and pdf.data == b"%PDF-1.4"


ISSUE = {
    "fields": {
        "summary": "Refund over limit needs approval",
        "issuetype": {"name": "Story"},
        "status": {"name": "In Progress"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Ana"},
        "reporter": {"displayName": "Raj"},
        "labels": ["payments", "approval"],
        "components": [{"name": "Billing"}],
        "description": "raw wiki",
        "subtasks": [{"key": "PAY-13", "fields": {"summary": "Add form"}}],
        "issuelinks": [
            {
                "type": {"name": "Blocks", "outward": "blocks"},
                "outwardIssue": {"key": "PAY-20", "fields": {"summary": "Ledger"}},
            }
        ],
        "attachment": [{"filename": "flow.png"}],
        "comment": {
            "comments": [{"author": {"displayName": "Lee"}, "created": "2026-10-01", "body": "raw"}]
        },
    },
    "renderedFields": {
        "description": "<p>Refunds above <b>500</b> need a manager.</p>",
        "comment": {"comments": [{"body": "<p>Agreed, limit is 500.</p>"}]},
    },
}


async def test_a_jira_issue_is_rendered_with_its_fields_links_and_comments(wire: Wire) -> None:
    wire.routes[("acme.atlassian.net", "/rest/api/2/issue/PAY-12")] = httpx.Response(
        200, json=ISSUE
    )
    env = {"TUVL_ATLASSIAN_EMAIL": "me@acme.test", "TUVL_ATLASSIAN_TOKEN": "tok"}
    got = await fetch("https://acme.atlassian.net/browse/PAY-12?focusedCommentId=1", env=env)
    text = got.data.decode()
    assert got.name == "jira-PAY-12.md" and got.origin["kind"] == "jira"
    assert text.startswith("# PAY-12: Refund over limit needs approval\n")
    for expected in (
        "- **Status:** In Progress",
        "- **Labels:** payments, approval",
        "Refunds above **500** need a manager.",
        "- PAY-13 Add form",
        "- blocks PAY-20 Ledger",
        "- flow.png",
        "### Lee, 2026-10-01",
        "Agreed, limit is 500.",
    ):
        assert expected in text
    sent = wire.seen[0]
    assert (
        sent.headers["authorization"] == "Basic " + base64.b64encode(b"me@acme.test:tok").decode()
    )
    assert "expand=renderedFields" in str(sent.url)


async def test_jira_credentials_go_only_to_atlassian_cloud_or_a_listed_host(wire: Wire) -> None:
    for host in ("jira.corp.example.com", "evil.example.org"):
        wire.routes[(host, "/rest/api/2/issue/PAY-1")] = httpx.Response(401)
    env = {"TUVL_ATLASSIAN_PAT": "secret-pat"}
    with pytest.raises(FetchError, match="TUVL_ATLASSIAN_HOSTS"):
        await fetch("https://evil.example.org/browse/PAY-1", env=env)
    assert "authorization" not in wire.seen[0].headers
    listed = {**env, "TUVL_ATLASSIAN_HOSTS": "jira.corp.example.com"}
    with pytest.raises(FetchError, match="asked for a login"):
        await fetch("https://jira.corp.example.com/browse/PAY-1", env=listed)
    assert wire.seen[1].headers["authorization"] == "Bearer secret-pat"


async def test_jira_without_credentials_says_which_to_set(wire: Wire) -> None:
    wire.routes[("acme.atlassian.net", "/rest/api/2/issue/PAY-1")] = httpx.Response(401)
    with pytest.raises(FetchError, match="TUVL_ATLASSIAN_EMAIL"):
        await fetch("https://acme.atlassian.net/browse/PAY-1", env={})


async def test_a_jira_board_link_and_a_context_path_find_the_issue(wire: Wire) -> None:
    wire.routes[("acme.atlassian.net", "/rest/api/2/issue/PAY-7")] = httpx.Response(200, json=ISSUE)
    wire.routes[("jira.corp.example.com", "/jira/rest/api/2/issue/PAY-8")] = httpx.Response(
        200, json=ISSUE
    )
    board = "https://acme.atlassian.net/jira/software/projects/PAY/boards/1?selectedIssue=PAY-7"
    a = await fetch(board, env={})
    b = await fetch("https://jira.corp.example.com/jira/browse/PAY-8", env={})
    assert a.name == "jira-PAY-7.md" and b.name == "jira-PAY-8.md"


async def test_a_confluence_page_is_rendered(wire: Wire) -> None:
    page = {
        "title": "Refund runbook",
        "space": {"name": "Payments"},
        "version": {"by": {"displayName": "Ana"}, "when": "2026-10-02"},
        "body": {
            "view": {"value": "<h2>Steps</h2><ol><li>Check the order</li><li>Approve</li></ol>"}
        },
    }
    wire.routes[("acme.atlassian.net", "/wiki/rest/api/content/123456")] = httpx.Response(
        200, json=page
    )
    got = await fetch(
        "https://acme.atlassian.net/wiki/spaces/PAY/pages/123456/Refund+runbook", env={}
    )
    text = got.data.decode()
    assert got.name == "confluence-123456-refund-runbook.md"
    assert text.startswith("# Refund runbook\n") and "- **Space:** Payments" in text
    assert "## Steps" in text and "1. Check the order" in text


async def test_a_github_issue_gets_its_comments_and_the_token_only_goes_to_the_api(
    wire: Wire,
) -> None:
    api = "api.github.com"
    wire.routes[(api, "/repos/acme/app/issues/42")] = httpx.Response(
        200,
        json={
            "title": "Export refunds as CSV",
            "state": "open",
            "user": {"login": "ana"},
            "labels": [{"name": "feature"}],
            "body": "We need a CSV.",
            "comments": 1,
            "created_at": "2026-10-01T10:00:00Z",
        },
    )
    wire.routes[(api, "/repos/acme/app/issues/42/comments")] = httpx.Response(
        200,
        json=[{"user": {"login": "raj"}, "created_at": "2026-10-02", "body": "+1, monthly please"}],
    )
    got = await fetch("https://github.com/acme/app/pull/42/files", env={"GITHUB_TOKEN": "ghp_x"})
    text = got.data.decode()
    assert got.name == "github-acme-app-42.md" and text.startswith(
        "# acme/app#42: Export refunds as CSV"
    )
    assert (
        "We need a CSV." in text and "### raj, 2026-10-02" in text and "+1, monthly please" in text
    )
    assert {r.url.host for r in wire.seen} == {PUBLIC}
    assert all(
        r.headers["authorization"] == "Bearer ghp_x" and r.headers["host"] == api for r in wire.seen
    )


async def test_a_private_github_repo_says_which_token_to_set(wire: Wire) -> None:
    wire.routes[("api.github.com", "/repos/acme/app/issues/1")] = httpx.Response(404)
    with pytest.raises(FetchError, match="TUVL_GITHUB_TOKEN"):
        await fetch("https://github.com/acme/app/issues/1", env={})


def test_repeated_names_are_numbered_and_files_written(wire: Wire, tmp_path: Path) -> None:
    wire.routes[("a.test", "/x")] = html("<p>Some words here ok.</p>")
    got = fetch_into(["https://a.test/x", "https://a.test/x"], tmp_path)
    assert [g.name for g in got] == ["web-a-test-x.md", "web-a-test-x-2.md"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["web-a-test-x-2.md", "web-a-test-x.md"]


def test_a_recorded_url_hides_secrets_and_user_info() -> None:
    url = safe_url("https://u:p@h.test/p?id=3&api_key=SECRET&sig=x#frag")
    assert url == "https://h.test/p?id=3&api_key=%E2%80%A6&sig=%E2%80%A6"
