import re

import fitz

PAGE_MARGIN = 50
MAX_CHUNK_CHARS = 1200
ORIGINAL_FONTSIZE = 9.5
TRANSLATION_FONTSIZE = 11
ORIGINAL_COLOR = (0.45, 0.45, 0.45)
TRANSLATION_COLOR = (0, 0, 0)
CJK_FONT = "china-t"

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？；;])\s+")


def parse_page_range(pages: str | None, total: int) -> list[int]:
    if pages is None or not pages.strip():
        return list(range(1, total + 1))
    selected: set[int] = set()
    for token in pages.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, _, end_s = token.partition("-")
            start = int(start_s) if start_s.strip() else 1
            end = int(end_s) if end_s.strip() else total
        else:
            start = end = int(token)
        if start < 1 or end < start or start > total:
            raise ValueError(f"invalid page range: {token}")
        selected.update(range(start, min(end, total) + 1))
    if not selected:
        raise ValueError("no pages selected")
    return sorted(selected)


def split_long_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remainder = text
    while len(remainder) > max_chars:
        window = remainder[:max_chars]
        cut = -1
        for match in _SENTENCE_BOUNDARY.finditer(window):
            cut = match.end()
        if cut < int(max_chars * 0.5):
            cut = max_chars
        chunks.append(remainder[:cut].rstrip())
        remainder = remainder[cut:].lstrip()
    if remainder:
        chunks.append(remainder)
    return chunks


def extract_paragraphs(doc: fitz.Document, page_numbers: list[int]) -> list[tuple[int, str]]:
    paragraphs: list[tuple[int, str]] = []
    for page_number in page_numbers:
        page = doc[page_number - 1]
        for block in page.get_text("blocks"):
            if block[6] != 0:
                continue
            text = " ".join(block[4].split())
            if not text:
                continue
            paragraphs.append((page_number, text))
    return paragraphs


def translate_paragraphs(
    paragraphs: list[tuple[int, str]],
    source_lang: str,
    target_lang: str,
    service,
) -> list[dict]:
    results = []
    for page_number, text in paragraphs:
        if source_lang == target_lang:
            result = {"translated_text": text, "source": "passthrough"}
        else:
            result = service.translate(text, source_lang, target_lang)
        results.append({"page": page_number, "original": text, **result})
    return results


class _PdfWriter:
    def __init__(self):
        self.doc = fitz.open()
        self.page = None
        self.y = PAGE_MARGIN

    def _new_page(self):
        self.page = self.doc.new_page(width=595, height=842)
        self.y = PAGE_MARGIN

    def insert(self, text: str, fontsize: float, color: tuple, gap_after: float):
        if not text:
            return
        for chunk in split_long_text(text):
            if self.page is None or self.y > 842 - PAGE_MARGIN - fontsize * 2:
                self._new_page()
            rect = fitz.Rect(PAGE_MARGIN, self.y, 595 - PAGE_MARGIN, 842 - PAGE_MARGIN)
            leftover = self.page.insert_textbox(
                rect, chunk, fontname=CJK_FONT, fontsize=fontsize, color=color, lineheight=1.4
            )
            if leftover < 0:
                self._new_page()
                rect = fitz.Rect(PAGE_MARGIN, self.y, 595 - PAGE_MARGIN, 842 - PAGE_MARGIN)
                leftover = self.page.insert_textbox(
                    rect, chunk, fontname=CJK_FONT, fontsize=fontsize, color=color, lineheight=1.4
                )
                if leftover < 0:
                    raise ValueError("paragraph too long to fit on one page")
            self.y = 842 - PAGE_MARGIN - leftover + gap_after


def build_output_pdf(results: list[dict], mode: str) -> bytes:
    writer = _PdfWriter()
    current_page = None
    for item in results:
        if item["page"] != current_page:
            current_page = item["page"]
            writer.insert(f"【第 {current_page} 頁】", 9, (0.3, 0.3, 0.3), 8)
        if mode == "compare":
            writer.insert(item["original"], ORIGINAL_FONTSIZE, ORIGINAL_COLOR, 4)
        writer.insert(item["translated_text"], TRANSLATION_FONTSIZE, TRANSLATION_COLOR, 14)
    return writer.doc.tobytes(deflate=True)


def translate_pdf(
    doc: fitz.Document,
    pages: str | None,
    source_lang: str,
    target_lang: str,
    service,
    mode: str = "compare",
) -> bytes:
    page_numbers = parse_page_range(pages, doc.page_count)
    paragraphs = extract_paragraphs(doc, page_numbers)
    if not paragraphs:
        raise ValueError("no extractable text found (scanned or image-based PDF?)")
    results = translate_paragraphs(paragraphs, source_lang, target_lang, service)
    return build_output_pdf(results, mode)
