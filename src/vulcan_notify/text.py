"""Shared text utilities."""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

import nh3


def strip_html(html: str) -> str:
    """Minimal HTML-to-text: strip tags, decode common entities, collapse whitespace."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_MESSAGE_STYLES = {
    "color",
    "background-color",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "text-align",
    "text-decoration",
    "text-indent",
    "white-space",
    "vertical-align",
    "line-height",
    "letter-spacing",
    "word-spacing",
    "margin",
    "margin-top",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "padding",
    "padding-top",
    "padding-bottom",
    "padding-left",
    "padding-right",
    "border",
    "border-width",
    "border-style",
    "border-color",
    "border-collapse",
    "border-spacing",
    "width",
    "height",
}


class _MessageHTMLParser(HTMLParser):
    """Remove implicit paragraph margins without overriding author styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "p":
            self.parts.append(self.get_starttag_text() or "")
            return
        values = dict(attrs)
        values["style"] = "margin-top:0;margin-bottom:0;" + (values.get("style") or "")
        attributes = "".join(
            f' {key}="{escape(value, quote=True)}"' if value is not None else f" {key}"
            for key, value in values.items()
        )
        self.parts.append(f"<p{attributes}>")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


def message_html(content: str, base_url: str | None = None) -> str:
    """Retain message layout and inline formatting as a sanitized HTML fragment."""
    attributes = {tag: values.copy() for tag, values in nh3.ALLOWED_ATTRIBUTES.items()}
    attributes.setdefault("*", set()).update({"style", "dir"})
    attributes["font"] = {"face", "color", "size"}
    for tag in ("table", "td", "th"):
        attributes.setdefault(tag, set()).update(
            {
                "width",
                "height",
                "align",
                "valign",
                "bgcolor",
                "border",
                "cellpadding",
                "cellspacing",
            }
        )
    cleaned = nh3.clean(
        content,
        tags=(nh3.ALLOWED_TAGS - {"img"}) | {"font"},
        clean_content_tags={"script", "style", "iframe", "object", "embed", "template"},
        attributes=attributes,
        filter_style_properties=_MESSAGE_STYLES,
        url_schemes={"http", "https", "mailto"},
        url_relative=("rewrite_with_base", base_url) if base_url else "deny",
    )
    # Plain message content can contain literal newlines instead of HTML breaks.
    if not nh3.is_html(content):
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    parser = _MessageHTMLParser()
    parser.feed(cleaned)
    parser.close()
    return "".join(parser.parts)


class _MessageTextParser(HTMLParser):
    """Render sanitized HTML as readable text with explicit block boundaries."""

    _BLOCKS = frozenset(
        {
            "div",
            "blockquote",
            "section",
            "article",
            "header",
            "footer",
            "dl",
            "dt",
            "dd",
            "ul",
            "ol",
            "li",
            "table",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }
    )
    _VOID = frozenset({"br", "hr", "wbr", "col", "area"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.lists: list[int | None] = []
        self.list_marker_pending = False

    def _break(self, count: int = 1) -> None:
        text = "".join(self.parts)
        if text:
            trailing = len(text) - len(text.rstrip("\n"))
            if trailing < count:
                self.parts.append("\n" * (count - trailing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        inherited = self.stack[-1][1] if self.stack else False
        preserve = (
            inherited
            or tag == "pre"
            or bool(
                re.search(
                    r"white-space:\s*(?:pre|pre-wrap|pre-line|break-spaces)(?:;|$)",
                    values.get("style") or "",
                )
            )
        )
        if tag not in self._VOID:
            self.stack.append((tag, preserve))
        if tag in self._BLOCKS or tag in {"p", "pre"}:
            if not (tag in {"p", "div"} and self.list_marker_pending):
                self._break()
        elif tag == "br":
            self.parts.append("\n")
            self.list_marker_pending = False
        elif tag == "hr":
            self._break()
            self.parts.append("---\n")
        if tag in {"ul", "ol"}:
            try:
                start = int(values.get("start") or "1") if tag == "ol" else None
            except ValueError:
                start = 1
            self.lists.append(start)
        elif tag == "li":
            number = self.lists[-1] if self.lists else None
            bullet = f"{number}. " if number is not None else "- "
            if number is not None:
                self.lists[-1] = number + 1
            self.parts.append("  " * max(0, len(self.lists) - 1) + bullet)
            self.list_marker_pending = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCKS or tag in {"p", "pre"}:
            self._break()
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        if tag in {"ul", "ol"} and self.lists:
            self.lists.pop()
        if tag == "li":
            self.list_marker_pending = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        # Editors also represent an intentionally empty line with a lone NBSP.
        if "\xa0" in data and not data.strip() and self.parts and self.parts[-1].endswith("\n"):
            self.parts.append("\n")
            return
        data = data.replace("\xa0", " ")
        if not self.stack or not self.stack[-1][1]:
            data = re.sub(r"\s+", " ", data)
            if not self.parts or self.parts[-1].endswith("\n"):
                data = data.lstrip()
        if data:
            self.parts.append(data)
            if data.strip():
                self.list_marker_pending = False


def message_text(content: str, base_url: str | None = None) -> str:
    """Keep paragraph, list, table and explicit line breaks in the plain version."""
    parser = _MessageTextParser()
    parser.feed(message_html(content, base_url))
    parser.close()
    return "\n".join(line.rstrip() for line in "".join(parser.parts).splitlines()).strip("\n")
