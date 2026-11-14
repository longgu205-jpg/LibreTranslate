import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz
import pytest

from agent.pdf_format import (
    find_broken_pages,
    page_integrity_ok,
    protect_formula_placeholders,
    restore_formula_placeholders,
    splice_replacement_pages,
)
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


def test_formula_placeholders_roundtrip():
    text = "The value {{v0}} equals {{v12}} here"
    prepared, mapping = protect_formula_placeholders(text)
    assert prepared == "The value __PDFPH_0__ equals __PDFPH_12__ here"
    assert restore_formula_placeholders(prepared, mapping) == text


def test_formula_placeholders_no_placeholder():
    prepared, mapping = protect_formula_placeholders("plain text")
    assert prepared == "plain text"
    assert mapping == {}


def _page_with_image_and_drawing(doc, text):
    page = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30))
    pix.set_rect(pix.irect, (200, 60, 60))
    page.insert_image(fitz.Rect(72, 72, 152, 132), pixmap=pix)
    page.draw_rect(fitz.Rect(200, 72, 320, 110), color=(0, 0, 0), width=1)
    page.insert_textbox(fitz.Rect(72, 150, 523, 300), text, fontname="helv", fontsize=11)
    return page


def test_page_integrity_ok_passes_identical_page():
    src = fitz.open()
    _page_with_image_and_drawing(src, "Hello")
    assert page_integrity_ok(src[0], src[0]) is True


def test_page_integrity_detects_lost_image():
    src = fitz.open()
    _page_with_image_and_drawing(src, "Hello")
    out = fitz.open()
    out.new_page(width=595, height=842).insert_textbox(
        fitz.Rect(72, 150, 523, 300), "譯文", fontname="china-t", fontsize=11
    )
    assert page_integrity_ok(src[0], out[0]) is False


def test_page_integrity_detects_lost_text():
    src = fitz.open()
    _page_with_image_and_drawing(src, "Hello")
    out = fitz.open()
    page = out.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30))
    pix.set_rect(pix.irect, (200, 60, 60))
    page.insert_image(fitz.Rect(72, 72, 152, 132), pixmap=pix)
    assert page_integrity_ok(src[0], out[0]) is False


def test_find_broken_pages_maps_to_positions():
    src = fitz.open()
    for i in range(3):
        _page_with_image_and_drawing(src, f"Page {i + 1}")
    out = fitz.open()
    for i in range(3):
        if i == 1:
            out.new_page(width=595, height=842)
        else:
            out.insert_pdf(src, from_page=i, to_page=i)
    broken = find_broken_pages(src, out, [1, 2, 3])
    assert broken == [1]


def test_splice_replacement_pages():
    out = fitz.open()
    for i in range(3):
        out.new_page(width=595, height=842).insert_textbox(
            fitz.Rect(72, 72, 523, 200), f"out {i}", fontname="helv", fontsize=11
        )
    comp = fitz.open()
    for i in range(2):
        comp.new_page(width=595, height=842).insert_textbox(
            fitz.Rect(72, 72, 523, 200), f"comp {i}", fontname="helv", fontsize=11
        )
    splice_replacement_pages(out, comp, [0, 2])
    assert out.page_count == 3
    assert "comp 0" in out[0].get_text()
    assert "out 1" in out[1].get_text()
    assert "comp 1" in out[2].get_text()
