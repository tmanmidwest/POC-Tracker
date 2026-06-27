"""Follow-ups: document text extraction + the Gemini provider."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.services.ai.base import GenerationError

# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _make_docx(paragraphs: list[str]) -> bytes:
    """Build a minimal valid .docx with the given paragraphs."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs
    )
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def test_extract_docx() -> None:
    from app.services.text_extract import extract_text

    data = _make_docx(["1.1 SSO via Okta", "1.2 MFA required"])
    text = extract_text("requirements.docx", data, None)
    assert "SSO via Okta" in text
    assert "MFA required" in text


def test_extract_xlsx() -> None:
    from openpyxl import Workbook

    from app.services.text_extract import extract_text

    wb = Workbook()
    ws = wb.active
    ws.append(["Ref", "Category", "Requirement"])
    ws.append(["1.1", "Access", "SSO via Okta"])
    ws.append(["1.2", "Access", "MFA required"])
    buf = io.BytesIO()
    wb.save(buf)

    text = extract_text("requirements.xlsx", buf.getvalue(), None)
    assert "SSO via Okta" in text
    assert "1.2 | Access | MFA required" in text


def test_extract_plain_text_passthrough() -> None:
    from app.services.text_extract import extract_text

    assert extract_text("notes.txt", b"hello world", "text/plain") == "hello world"


def test_extract_bad_docx_raises() -> None:
    from app.services.text_extract import TextExtractError, extract_text

    with pytest.raises(TextExtractError):
        extract_text("broken.docx", b"not a zip", None)


def test_extract_empty_is_empty() -> None:
    from app.services.text_extract import extract_text

    assert extract_text("x.txt", b"", None) == ""


# ---------------------------------------------------------------------------
# Gemini provider (httpx mocked)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def test_gemini_generate(monkeypatch) -> None:
    import app.services.ai.gemini_provider as g

    def fake_post(url, json, headers, timeout):
        assert headers["x-goog-api-key"] == "k"
        assert "models/gemini-2.5-flash:generateContent" in url
        assert json["systemInstruction"]["parts"][0]["text"] == "sys"
        return _FakeResp(200, {"candidates": [{"content": {"parts": [{"text": "Hi there"}]}}]})

    monkeypatch.setattr(g.httpx, "post", fake_post)
    out = g.generate(api_key="k", model="gemini-2.5-flash", system="sys", prompt="p")
    assert out == "Hi there"


def test_gemini_blocked_raises(monkeypatch) -> None:
    import app.services.ai.gemini_provider as g

    def fake_post(url, json, headers, timeout):
        return _FakeResp(200, {"promptFeedback": {"blockReason": "SAFETY"}})

    monkeypatch.setattr(g.httpx, "post", fake_post)
    with pytest.raises(GenerationError, match="SAFETY"):
        g.generate(api_key="k", model="gemini-2.5-flash", system="s", prompt="p")


def test_gemini_bad_key_message(monkeypatch) -> None:
    import app.services.ai.gemini_provider as g

    def fake_post(url, json, headers, timeout):
        return _FakeResp(400, {"error": {"message": "API key not valid"}})

    monkeypatch.setattr(g.httpx, "post", fake_post)
    with pytest.raises(GenerationError, match="rejected the API key"):
        g.generate(api_key="bad", model="gemini-2.5-flash", system="s", prompt="p")


def test_gemini_registered_and_implemented() -> None:
    from app.services.ai.registry import PROVIDERS

    spec = PROVIDERS["google"]
    assert spec.implemented is True
    assert spec.generate is not None and spec.stream is not None
