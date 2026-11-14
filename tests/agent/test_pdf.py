import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz
import pytest

from agent.pdf_translator import (
    build_output_pdf,
    extract_paragraphs,
    parse_page_range,
    split_long_text,
    translate_paragraphs,
    translate_pdf,
)


class StubService:
    def translate(self, text, source_lang, target_lang):
        return {
            "translated_text": f"[譯]{text}",
            "source": "cloud",
            "model": "test-model",
            "source_lang": source_lang,
            "target_lang": target_lang,
        }


@pytest.fixture
def sample_pdf():
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(
            fitz.Rect(50, 50, 545, 800),
            f"Page {i + 1} first paragraph.\n\nPage {i + 1} second paragraph.",
            fontname="helv",
            fontsize=12,
        )
    data = doc.tobytes()
    doc.close()
    return fitz.open(stream=data, filetype="pdf")


def test_parse_page_range_defaults_to_all(sample_pdf):
    assert parse_page_range(None, 3) == [1, 2, 3]
    assert parse_page_range("", 3) == [1, 2, 3]


def test_parse_page_range_single_and_range(sample_pdf):
    assert parse_page_range("2", 3) == [2]
    assert parse_page_range("2-3", 3) == [2, 3]
    assert parse_page_range("2-", 3) == [2, 3]
    assert parse_page_range("-2", 3) == [1, 2]


def test_parse_page_range_comma_and_clamp():
    assert parse_page_range("1,3", 3) == [1, 3]
    assert parse_page_range("2-9", 3) == [2, 3]


@pytest.mark.parametrize("spec", ["0", "5-2", "99", "abc"])
def test_parse_page_range_invalid(spec):
    with pytest.raises(ValueError):
        parse_page_range(spec, 3)


def test_split_long_text_short_passthrough():
    assert split_long_text("short text") == ["short text"]


def test_split_long_text_chunks_within_limit():
    text = ". ".join(f"Sentence number {i} with some words" for i in range(200))
    chunks = split_long_text(text, max_chars=1200)
    assert all(len(chunk) <= 1200 for chunk in chunks)
    assert " ".join(chunk.strip() for chunk in chunks) == text


def test_extract_paragraphs(sample_pdf):
    paragraphs = extract_paragraphs(sample_pdf, [1, 2])
    pages = [page for page, _ in paragraphs]
    assert set(pages) == {1, 2}
    assert any("Page 1 first paragraph" in text for _, text in paragraphs)
    assert not any("Page 3" in text for _, text in paragraphs)


def test_translate_paragraphs_uses_service():
    paragraphs = [(1, "Hello"), (2, "World")]
    results = translate_paragraphs(paragraphs, "en", "zh-TW", StubService())
    assert len(results) == 2
    assert results[0]["translated_text"] == "[譯]Hello"
    assert results[0]["page"] == 1


def test_translate_paragraphs_passthrough_same_lang():
    results = translate_paragraphs([(1, "文字")], "zh", "zh", StubService())
    assert results[0]["translated_text"] == "文字"
    assert results[0]["source"] == "passthrough"


def test_build_output_pdf_contains_original_and_translation():
    results = [
        {"page": 1, "original": "Hello world", "translated_text": "哈囉，世界！"},
    ]
    pdf_bytes = build_output_pdf(results, "compare")
    out = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = out[0].get_text()
    assert "Hello world" in text
    assert "哈囉，世界！" in text
    assert "第 1 頁" in text


def test_build_output_pdf_translation_only_skips_original():
    results = [
        {"page": 1, "original": "Hello world", "translated_text": "哈囉，世界！"},
    ]
    pdf_bytes = build_output_pdf(results, "translation_only")
    out = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = out[0].get_text()
    assert "哈囉，世界！" in text
    assert "Hello world" not in text


def test_translate_pdf_with_page_range(sample_pdf):
    pdf_bytes = translate_pdf(sample_pdf, "2", "en", "zh-TW", StubService())
    out = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(page.get_text() for page in out)
    assert "Page 2" in text
    assert "Page 1" not in text
    assert "Page 3" not in text
