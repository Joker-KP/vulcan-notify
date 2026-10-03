"""Shared bundled HTML templates for email notifications and change digests."""

from functools import lru_cache
from html import escape
from importlib.resources import files
from string import Template


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
