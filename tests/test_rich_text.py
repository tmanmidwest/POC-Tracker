"""Tests for the rich-text note sanitizer and plain-text rendering.

The sanitizer is the security boundary for note HTML: anything it returns is
rendered with ``| safe`` in the UI and PDF, so these tests pin down both the
allow-list (formatting survives) and the deny-list (scripts/handlers/unsafe
URLs are stripped).
"""

from __future__ import annotations

from app.services.rich_text import html_to_text, sanitize_note_html


def test_keeps_basic_formatting_and_lists() -> None:
    html = sanitize_note_html(
        "<p>Hi <strong>there</strong> <em>all</em></p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<ol><li>first</li></ol>"
    )
    assert html == (
        "<p>Hi <strong>there</strong> <em>all</em></p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<ol><li>first</li></ol>"
    )


def test_strips_script_tag() -> None:
    html = sanitize_note_html("<p>ok</p><script>alert(1)</script>")
    assert "script" not in (html or "").lower()
    assert html == "<p>ok</p>"


def test_strips_inline_event_handlers() -> None:
    html = sanitize_note_html('<p onclick="evil()">click</p>')
    assert "onclick" not in (html or "")
    assert html == "<p>click</p>"


def test_drops_javascript_url_scheme() -> None:
    html = sanitize_note_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript" not in (html or "")


def test_keeps_https_link_and_adds_rel() -> None:
    html = sanitize_note_html('<a href="https://example.com">docs</a>') or ""
    assert 'href="https://example.com"' in html
    assert "noopener" in html  # rel hardening applied


def test_strips_disallowed_tags_keeping_text() -> None:
    # Headings/tables are outside the allow-list; their text content remains.
    html = sanitize_note_html("<h1>Title</h1><table><tr><td>cell</td></tr></table>") or ""
    assert "<h1>" not in html and "<table>" not in html
    assert "Title" in html and "cell" in html


def test_empty_or_whitespace_only_returns_none() -> None:
    assert sanitize_note_html("") is None
    assert sanitize_note_html("   ") is None
    assert sanitize_note_html("<p><br></p>") is None
    assert sanitize_note_html("<p>   </p>") is None


def test_html_to_text_bullets_and_breaks() -> None:
    text = html_to_text("<p>Intro</p><ul><li>a</li><li>b</li></ul>")
    assert text == "Intro\n• a\n• b"


def test_html_to_text_unescapes_entities() -> None:
    assert html_to_text("<p>a &amp; b &lt;c&gt;</p>") == "a & b <c>"


def test_html_to_text_none_passthrough() -> None:
    assert html_to_text(None) is None
    assert html_to_text("") is None
