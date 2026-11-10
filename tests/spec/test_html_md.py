from tuvl_insight.spec.html_md import html_to_markdown


def md(html: str, **kw: bool) -> str:
    return html_to_markdown(html, **kw)[0]


def test_headings_paragraphs_links_and_emphasis() -> None:
    out = md(
        '<h1>Refunds</h1><p>A <b>customer</b> asks, see <a href="https://x.test/a">the policy</a>'
        " and <em>wait</em> <code>14d</code>.</p><h2>Rules</h2>"
    )
    assert (
        out
        == "# Refunds\n\nA **customer** asks, see [the policy](https://x.test/a) and *wait* `14d`.\n\n## Rules\n"
    )


def test_lists_nest_and_number() -> None:
    out = md(
        "<ul><li>one<ul><li>inner</li></ul></li><li>two</li></ul><ol><li>a</li><li>b</li></ol>"
    )
    assert out.splitlines() == ["- one", "  - inner", "- two", "", "1. a", "2. b"]


def test_a_table_becomes_a_markdown_table() -> None:
    out = md(
        "<table><tr><th>Field</th><th>Type</th></tr><tr><td>id</td><td>uuid | pk</td></tr></table>"
    )
    assert out.splitlines() == ["| Field | Type |", "| --- | --- |", "| id | uuid \\| pk |"]


def test_preformatted_text_keeps_its_lines() -> None:
    assert md("<pre>a = 1\n  b = 2</pre>") == "```\na = 1\n  b = 2\n```\n"


def test_scripts_styles_and_page_chrome_are_dropped() -> None:
    page = (
        "<html><head><title>T</title><style>p{}</style><script>var x=1</script></head><body>"
        "<nav>Home Pricing</nav><header>Site</header><p>Real text here.</p>"
        "<form><input></form><footer>Copyright</footer></body></html>"
    )
    assert md(page) == "Real text here.\n"


def test_main_wins_over_the_rest_of_the_page() -> None:
    page = (
        "<body><p>Sidebar blurb</p><main><h1>Spec</h1><p>Body text.</p></main><p>Related</p></body>"
    )
    assert md(page) == "# Spec\n\nBody text.\n"


def test_keep_chrome_keeps_header_and_nav_for_api_html() -> None:
    assert "Site" in md("<header>Site</header><p>x y z</p>", keep_chrome=True)


def test_the_title_is_returned_and_unclosed_tags_do_not_raise() -> None:
    out, title = html_to_markdown("<title> My  page </title><p>one<p>two <b>bold")
    assert title == "My page"
    assert "one" in out and "two" in out
