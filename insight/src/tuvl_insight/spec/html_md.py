"""HTML to Markdown for idea snapshots: headings, paragraphs, lists, links, code, quotes and tables.

Stdlib only. A page with a ``<main>`` or ``<article>`` keeps just that; otherwise the body without its
navigation, header, footer, aside and forms. Scripts, styles and embedded objects are always dropped.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_DROP = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "head",
}
_CHROME = {"nav", "footer", "aside", "form", "header", "dialog"}
_BLOCK = {
    "p", "div", "section", "article", "main", "ul", "ol", "li", "table", "tr", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dl", "dt", "dd", "figure", "figcaption", "details",
}  # fmt: skip
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "source", "wbr"}
_MAIN = re.compile(r"<(main|article)\b", re.I)


class _Converter(HTMLParser):
    def __init__(self, *, only_main: bool, keep_chrome: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._only_main = only_main
        self._keep_chrome = keep_chrome
        self._out: list[str] = []
        self._skip = 0
        self._main_depth = 0
        self._main_done = False
        self._stack: list[str] = []
        self._lists: list[list[int]] = []  # [next number, 0 for a bullet list]
        self._pre = 0
        self._href: list[str | None] = []
        self._cells: list[str] | None = None
        self._row_cells: list[list[str]] = []
        self._in_title = False

    # keeping output ---------------------------------------------------------

    @property
    def _live(self) -> bool:
        if self._skip:
            return False
        return not self._only_main or self._main_depth > 0

    def _emit(self, text: str) -> None:
        if not self._live:
            return
        if self._cells is not None:
            self._cells.append(text)
        else:
            self._out.append(text)

    def _break(self, blank: bool = True) -> None:
        if not self._live or self._cells is not None:
            return
        if blank:
            self._out.append("\n\n")
        elif not (self._out and self._out[-1].endswith("\n")):
            self._out.append("\n")

    # parser callbacks -------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        a = dict(attrs)
        if tag in _VOID:
            self._void(tag, a)
            return
        self._stack.append(tag)
        if tag in _DROP or (tag in _CHROME and not self._keep_chrome and not self._main_depth):
            self._skip += 1
            return
        if self._only_main and tag in ("main", "article") and not self._main_done:
            self._main_depth += 1
        self._open(tag, a)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            self._void(tag, dict(attrs))
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _VOID or tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            self._close(top)
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = " ".join(data.split())
        if not self._live:
            return
        if self._pre:
            self._emit(data)
        else:
            text = re.sub(r"\s+", " ", data)
            if text.strip() or (self._out and not self._out[-1].endswith(("\n", " "))):
                self._emit(text)

    # element handling -------------------------------------------------------

    def _void(self, tag: str, a: dict[str, str | None]) -> None:
        if tag == "br":
            self._emit("  \n" if not self._cells else " ")
        elif tag == "hr":
            self._break()
            self._emit("---")
            self._break()
        elif tag == "img" and a.get("alt"):
            self._emit(f"[{a['alt']}]")

    def _open(self, tag: str, a: dict[str, str | None]) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._break()
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._break(blank=not self._lists)
            self._lists.append([1, 0 if tag == "ul" else 1])
        elif tag == "li":
            depth = max(len(self._lists) - 1, 0)
            self._break(blank=False)
            marker = "- "
            if self._lists and self._lists[-1][1]:
                marker = f"{self._lists[-1][0]}. "
                self._lists[-1][0] += 1
            self._emit("  " * depth + marker)
        elif tag == "pre":
            self._break()
            self._emit("```\n")
            self._pre += 1
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "blockquote":
            self._break()
            self._emit("> ")
        elif tag == "a":
            self._href.append(a.get("href"))
            self._emit("[")
        elif tag == "table":
            self._break()
            self._row_cells = []
        elif tag in ("td", "th"):
            self._cells = []
        elif tag in _BLOCK:
            self._break()

    def _close(self, tag: str) -> None:
        if tag in _DROP or (tag in _CHROME and not self._keep_chrome and not self._main_depth):
            self._skip = max(self._skip - 1, 0)
            return
        if tag in ("main", "article") and self._only_main and self._main_depth:
            self._main_depth -= 1
            if not self._main_depth:
                self._main_done = True
        if tag in (
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "p",
            "div",
            "section",
            "blockquote",
            "figure",
        ):
            self._break()
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._break(blank=not self._lists)
        elif tag == "pre":
            self._pre = max(self._pre - 1, 0)
            self._emit("\n```")
            self._break()
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a":
            href = self._href.pop() if self._href else None
            self._emit(f"]({href})" if href and not href.startswith(("javascript:", "#")) else "]")
        elif tag in ("td", "th"):
            cell = " ".join("".join(self._cells or []).split()).replace("|", "\\|")
            self._cells = None
            if self._row_cells:
                self._row_cells[-1].append(cell)
            else:
                self._row_cells.append([cell])
        elif tag == "tr":
            self._row_cells.append([])
        elif tag == "table":
            self._flush_table()

    def _flush_table(self) -> None:
        rows = [r for r in self._row_cells if r]
        self._row_cells = []
        if not rows or not self._live:
            return
        width = max(len(r) for r in rows)
        lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
        lines.insert(1, "|" + " --- |" * width)
        self._out.append("\n".join(lines))
        self._break()

    def result(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n" if text.strip() else ""


def html_to_markdown(html: str, *, keep_chrome: bool = False) -> tuple[str, str]:
    """``(markdown, page title)``. ``keep_chrome`` keeps header, nav and footer (for API-rendered HTML)."""
    parser = _Converter(
        only_main=bool(_MAIN.search(html)) and not keep_chrome, keep_chrome=keep_chrome
    )
    parser.feed(html)
    parser.close()
    return parser.result(), parser.title
