"""Shared bundled HTML templates for email notifications and change digests."""

from functools import lru_cache
from html import escape
from importlib.resources import files
from string import Template

from markdown_it import MarkdownIt

from vulcan_notify.text import message_html


def markdown_html(content: str) -> str:
    """Render AI Markdown with inline email styles and the shared sanitizer."""
    parser = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")
    tokens = parser.parse(content)
    styles = {
        "heading_open": "margin:16px 0 8px;font-size:18px;color:#0f172a",
        "paragraph_open": "margin:0 0 12px",
        "bullet_list_open": "margin:0 0 12px;padding-left:24px",
        "ordered_list_open": "margin:0 0 12px;padding-left:24px",
        "list_item_open": "margin:4px 0",
        "blockquote_open": "margin:12px 0;padding-left:12px;border-left:3px solid #cbd5e1",
        "table_open": "margin:12px 0;border-collapse:collapse;width:100%",
        "th_open": "padding:6px 8px;border:1px solid #e2e8f0;text-align:left",
        "td_open": "padding:6px 8px;border:1px solid #e2e8f0;text-align:left",
    }
    for token in tokens:
        # The card and section already own h1/h2; AI headings belong beneath them.
        if token.type in {"heading_open", "heading_close"} and token.tag in {"h1", "h2"}:
            token.tag = "h3"
        if token.type in styles:
            token.attrSet("style", styles[token.type])
    return message_html(str(parser.renderer.render(tokens, parser.options, {})))


@lru_cache
def _template(name: str) -> Template:
    source = files("vulcan_notify").joinpath("email_templates", f"{name}.html")
    return Template(source.read_text(encoding="utf-8"))


def render_template(name: str, **values: str) -> str:
    """Insert already escaped text or sanitized HTML into a trusted template."""
    return _template(name).substitute(values)


def render_email(heading: str, content: str, *, heading_context: str = "") -> str:
    """Wrap each notification type in the same layout."""
    context = (
        f'<p style="margin:4px 0;color:#64748b;font-size:14px">{escape(heading_context)}</p>'
        if heading_context
        else ""
    )
    return render_template(
        "layout", heading=escape(heading), heading_context=context, content=content
    )
