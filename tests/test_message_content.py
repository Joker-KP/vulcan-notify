"""Original message layout in HTML and readable text alternatives."""

from html import escape

import pytest

from vulcan_notify.text import message_html, message_text


@pytest.mark.parametrize(
    "source,expected",
    [
        ("<p>Pierwszy akapit</p><p>Drugi akapit</p>", "Pierwszy akapit\nDrugi akapit"),
        ("<p>A<br></p><p>B</p>", "A\nB"),
        ("<p>A</p><p><br></p><p>B</p>", "A\n\nB"),
        ("<p>A</p><p>&nbsp;</p><p>B</p>", "A\n\nB"),
        ("<ul><li><p>A</p></li><li><p>B</p></li></ul>", "- A\n- B"),
        ("<div>A</div><div>B<BR />C<br><br>D</div>", "A\nB\nC\n\nD"),
        ("<ul><li>Pierwszy</li><li>Drugi</li></ul>", "- Pierwszy\n- Drugi"),
        ('<ol start="3"><li>Pierwszy</li><li>Drugi</li></ol>', "3. Pierwszy\n4. Drugi"),
        ("<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>", "A\tB\nC\tD"),
        ("<p>Zaż&oacute;łć&nbsp;&amp;&nbsp;&quot;test&quot;&#33;</p>", 'Zażółć & "test"!'),
        ("<pre>  code\n    indent</pre>", "  code\n    indent"),
        ('<div style="white-space:pre-wrap">A\n  B</div>', "A\n  B"),
        (
            "Zwykły tekst\nDruga linia\n\nCzwarta linia",
            "Zwykły tekst\nDruga linia\n\nCzwarta linia",
        ),
        (
            "<p>First <strong>important</strong> words</p>\n<p>Next</p>",
            "First important words\nNext",
        ),
    ],
)
def test_text_alternative_preserves_visible_structure(source, expected):
    assert message_text(source) == expected


def test_html_keeps_paragraphs_lists_tables_and_inline_styles():
    source = (
        '<p style="margin-bottom:12px;color:red;font-size:16px">Dzień dobry,</p>'
        "<div>Pierwsza linia<br>Druga linia</div>"
        "<ul><li><strong>Pogrubienie</strong> i <em>kursywa</em></li></ul>"
        "<table><tr><td>Treść</td></tr></table>"
        '<a href="/testdistrict/App/odebrane">Skrzynka</a>'
    )
    rendered = message_html(source, "https://wiadomosci.eduvulcan.pl/testdistrict/App/odebrane")
    for fragment in [
        "<p style=",
        "margin-bottom:12px",
        "color:red",
        "font-size:16px",
        "<div>Pierwsza linia<br>Druga linia</div>",
        "<ul><li>",
        "<strong>Pogrubienie</strong>",
        "<em>kursywa</em>",
        "<table>",
        "<td>Treść</td>",
    ]:
        assert fragment in rendered
    assert 'href="https://wiadomosci.eduvulcan.pl/testdistrict/App/odebrane"' in rendered


def test_html_avoids_default_paragraph_gaps_and_keeps_explicit_spacing():
    rendered = message_html(
        '<p dir="rtl">A &amp; B</p><p><br></p><p style="margin-bottom:12px;color:red">B</p>'
    )
    assert '<p dir="rtl" style="margin-top:0;margin-bottom:0;">A &amp; B</p>' in rendered
    assert '<p style="margin-top:0;margin-bottom:0;"><br></p>' in rendered
    assert 'style="margin-top:0;margin-bottom:0;margin-bottom:12px;color:red"' in rendered


def test_html_drops_active_content_without_losing_message_text():
    source = (
        '<p onclick="alert(1)" style="color:red;position:fixed;background-image:url(https://example.org/x)">'
        "Ważna treść</p><script>secret_script()</script>"
        '<iframe src="https://example.org/frame">hidden_frame</iframe>'
        '<img src="https://example.org/pixel" onerror="alert(1)">'
        '<a href="jav&#x61;script:alert(1)">Zły link</a>'
    )
    rendered = message_html(source)
    for unwanted in [
        "onclick",
        "onerror",
        "<script",
        "secret_script",
        "<iframe",
        "hidden_frame",
        "<img",
        "background-image",
        "position:",
        "javascript:",
    ]:
        assert unwanted not in rendered
    assert "Ważna treść" in rendered and "color:red" in rendered
    assert message_text(source) == "Ważna treść\nZły link"


@pytest.mark.parametrize(
    "source,destination,label,suffix",
    [
        ("http://x.pl", "http://x.pl", "x.pl", ""),
        ("www.example.org/path", "https://www.example.org/path", "www.example.org/path", ""),
        (
            "HTTPS://example.org/long-resource",
            "HTTPS://example.org/long-resource",
            "example.org/long-res...",
            "",
        ),
        (
            "https://example.org/search?q=one&amp;page=2.",
            "https://example.org/search?q=one&page=2",
            "example.org/search?q...",
            ".",
        ),
        (
            "(https://example.org/info).",
            "https://example.org/info",
            "example.org/info",
            ").",
        ),
        (
            "https://example.org/wiki/Test_(A).",
            "https://example.org/wiki/Test_(A)",
            "example.org/wiki/Tes...",
            ".",
        ),
    ],
)
def test_html_links_websites_with_full_destinations_and_short_labels(
    source, destination, label, suffix
):
    rendered = message_html(source, linkify=True)
    href = escape(destination, quote=True)
    assert f'href="{href}" title="{href}"' in rendered
    assert f">{label}</a>{suffix}" in rendered
    assert rendered.count("<a ") == 1
    assert "<a " not in message_html(source)
    assert "..." not in message_text(source)


def test_linkification_preserves_existing_links_formatting_and_sanitization():
    source = (
        "<p><strong>Website:</strong> "
        "https://example.org/plain?x=1&amp;y=2 &lt;test&gt;</p>"
        '<a href="https://example.org/original" title="https://example.org/attribute">'
        "<em>https://example.org/label</em></a>"
        "<script>https://example.org/hidden</script>"
        '<img src="https://example.org/pixel">'
        '<a href="javascript:alert(1)">Unsafe</a>'
    )
    rendered = message_html(source, linkify=True)
    assert rendered.count("<a ") == 3  # One new link plus both original anchors.
    assert "<em>https://example.org/label</em></a>" in rendered
    assert 'href="https://example.org/original"' in rendered
    assert "/attribute" not in rendered  # Sanitizer removes unsupported attributes.
    assert "<strong>Website:</strong>" in rendered
    assert "&lt;test&gt;" in rendered
    for removed in ["<script", "/hidden", "<img", "/pixel", "javascript:"]:
        assert removed not in rendered


def test_linkification_leaves_non_websites_and_malformed_urls_as_text():
    source = "mail@example.org ftp://example.org javascript:alert(1) https:// www. https://[broken"
    rendered = message_html(source, linkify=True)
    assert "<a " not in rendered
    assert rendered == source
